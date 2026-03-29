#!/usr/bin/env python3
"""
Virtual OCPP 1.6j EV Charger Simulator
Connects to an OCPP central system and simulates realistic DC fast charging.
"""

import asyncio
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import websockets
from ocpp.v16 import ChargePoint as CP
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    Measurand,
    RegistrationStatus,
    RemoteStartStopStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnitOfMeasure,
)
from ocpp.routing import on

# ─── Configuration ───────────────────────────────────────────────────────────

OCPP_URL = os.environ.get("OCPP_URL", "ws://host.docker.internal:9100/ocpp/VIRT-001")
CP_ID = os.environ.get("CP_ID", "VIRT-001")
VENDOR = os.environ.get("VENDOR", "VirtualCharger")
MODEL = os.environ.get("MODEL", "Virtual-120kW")
SERIAL = os.environ.get("SERIAL", f"{CP_ID}-SN")
FIRMWARE = os.environ.get("FIRMWARE", "VIRT-1.0.0")
MAX_KW = float(os.environ.get("MAX_KW", "120"))
NUM_CONNECTORS = int(os.environ.get("NUM_CONNECTORS", "2"))
INITIAL_SOC = int(os.environ.get("INITIAL_SOC", "0"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
METER_INTERVAL = int(os.environ.get("METER_INTERVAL", "30"))
AUTO_START = os.environ.get("AUTO_START", "false").lower() in ("true", "1", "yes")
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5"))

NOMINAL_VOLTAGE = 400.0  # DC voltage

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(CP_ID)

# ─── Charging Session State ──────────────────────────────────────────────────


class ChargingSession:
    """Tracks state for one connector's active charging session."""

    def __init__(self, connector_id: int, transaction_id: int, meter_start: int, soc: float):
        self.connector_id = connector_id
        self.transaction_id = transaction_id
        self.meter_start = meter_start
        self.meter_wh = float(meter_start)
        self.soc = soc
        self.start_time = time.monotonic()
        self.active = True
        self.stop_requested = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def compute_power_kw(self, max_kw: float) -> float:
        """Compute instantaneous power based on realistic DC fast charge curve."""
        elapsed = self.elapsed
        soc = self.soc

        if elapsed < 30:
            # Ramp phase: 0 → max_kw over 30s
            base = max_kw * (elapsed / 30.0)
        elif soc < 80:
            # Bulk phase: constant max_kw
            base = max_kw
        elif soc < 95:
            # Taper phase: linear decrease from max_kw to max_kw*0.3
            fraction = (soc - 80) / 15.0
            base = max_kw * (1.0 - 0.7 * fraction)
        else:
            # Trickle phase: max_kw * 0.1
            base = max_kw * 0.1

        # Add ±2% jitter
        jitter = random.uniform(-0.02, 0.02)
        return max(0, base * (1 + jitter))

    def tick(self, dt: float, max_kw: float) -> dict:
        """Advance the simulation by dt seconds. Returns meter snapshot."""
        power_kw = self.compute_power_kw(max_kw)
        energy_kwh = power_kw * (dt / 3600.0)
        self.meter_wh += energy_kwh * 1000.0

        # Battery capacity approximation: 75 kWh typical EV
        battery_kwh = 75.0
        self.soc += (energy_kwh / battery_kwh) * 100.0
        self.soc = min(100.0, self.soc)

        voltage = NOMINAL_VOLTAGE * random.uniform(0.95, 1.05)
        power_w = power_kw * 1000.0
        current_a = power_w / voltage if voltage > 0 else 0

        return {
            "power_w": round(power_w, 1),
            "energy_wh": round(self.meter_wh),
            "current_a": round(current_a, 1),
            "voltage": round(voltage, 1),
            "soc": round(self.soc, 1),
        }


# ─── Charge Point ────────────────────────────────────────────────────────────


class VirtualCharger(CP):
    """OCPP 1.6j virtual charge point."""

    def __init__(self, cp_id: str, connection):
        super().__init__(cp_id, connection)
        self.connector_status: dict[int, str] = {}
        self.sessions: dict[int, ChargingSession] = {}
        self.cumulative_meter: dict[int, float] = {}
        self._heartbeat_task = None
        self._meter_tasks: dict[int, asyncio.Task] = {}
        self._config = {
            "HeartbeatInterval": str(HEARTBEAT_INTERVAL),
            "MeterValueSampleInterval": str(METER_INTERVAL),
            "NumberOfConnectors": str(NUM_CONNECTORS),
            "ChargePointVendor": VENDOR,
            "ChargePointModel": MODEL,
            "ChargePointSerialNumber": SERIAL,
            "FirmwareVersion": FIRMWARE,
        }

    # ─── Boot sequence ───────────────────────────────────────────────────

    async def boot(self):
        """Run the full boot sequence."""
        # BootNotification
        resp = await self.call(
            call.BootNotificationPayload(
                charge_point_vendor=VENDOR,
                charge_point_model=MODEL,
                charge_point_serial_number=SERIAL,
                firmware_version=FIRMWARE,
            )
        )
        if resp.status == RegistrationStatus.accepted:
            log.info("BootNotification accepted (interval=%ds)", resp.interval)
        else:
            log.warning("BootNotification status: %s", resp.status)

        # StatusNotification for each connector
        for conn_id in range(0, NUM_CONNECTORS + 1):
            status = ChargePointStatus.available if conn_id > 0 else ChargePointStatus.available
            await self.send_status(conn_id, status)

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Auto-start if configured
        if AUTO_START:
            await asyncio.sleep(2)
            for conn_id in range(1, NUM_CONNECTORS + 1):
                await self._start_charging(conn_id, "AUTO-TAG")
                break  # Only auto-start on connector 1

    async def send_status(self, connector_id: int, status: str):
        """Send StatusNotification and track state."""
        self.connector_status[connector_id] = status
        await self.call(
            call.StatusNotificationPayload(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=status,
            )
        )
        log.info("Connector %d → %s", connector_id, status)

    async def _heartbeat_loop(self):
        """Send periodic heartbeats."""
        interval = int(self._config.get("HeartbeatInterval", HEARTBEAT_INTERVAL))
        while True:
            await asyncio.sleep(interval)
            try:
                resp = await self.call(call.HeartbeatPayload())
                log.debug("Heartbeat → %s", resp.current_time)
            except Exception as e:
                log.warning("Heartbeat failed: %s", e)
                break

    # ─── Charging logic ──────────────────────────────────────────────────

    async def _start_charging(self, connector_id: int, id_tag: str):
        """Start a charging session on a connector."""
        if connector_id in self.sessions:
            log.warning("Connector %d already charging", connector_id)
            return

        # Preparing phase
        await self.send_status(connector_id, ChargePointStatus.preparing)
        await asyncio.sleep(2)

        # Determine starting SoC
        if INITIAL_SOC > 0:
            soc = float(INITIAL_SOC)
        else:
            soc = random.uniform(15, 45)

        # Meter start (cumulative, never resets)
        meter_start = int(self.cumulative_meter.get(connector_id, random.randint(0, 50000)))

        # StartTransaction
        resp = await self.call(
            call.StartTransactionPayload(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=meter_start,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        transaction_id = resp.transaction_id
        log.info(
            "StartTransaction connector=%d txn=%d meter_start=%d soc=%.1f%%",
            connector_id, transaction_id, meter_start, soc,
        )

        # Create session
        session = ChargingSession(connector_id, transaction_id, meter_start, soc)
        self.sessions[connector_id] = session

        # Charging status
        await self.send_status(connector_id, ChargePointStatus.charging)

        # Start meter values loop
        self._meter_tasks[connector_id] = asyncio.create_task(
            self._meter_loop(session)
        )

    async def _meter_loop(self, session: ChargingSession):
        """Send MeterValues periodically and stop when done."""
        interval = int(self._config.get("MeterValueSampleInterval", METER_INTERVAL))
        try:
            while session.active and session.soc < 100.0:
                await asyncio.sleep(interval)
                if not session.active:
                    break

                snap = session.tick(interval, MAX_KW)
                await self._send_meter_values(session.connector_id, session.transaction_id, snap)

                log.info(
                    "Connector %d: %.1f kW | %.1f%% SoC | %d Wh",
                    session.connector_id,
                    snap["power_w"] / 1000,
                    snap["soc"],
                    snap["energy_wh"],
                )

                if session.soc >= 100.0:
                    log.info("Connector %d: SoC 100%% reached", session.connector_id)
                    await self._stop_charging(session.connector_id, "Local")
                    return

                if session.stop_requested:
                    await self._finish_stop(session)
                    return

        except asyncio.CancelledError:
            pass

    async def _send_meter_values(self, connector_id: int, transaction_id: int, snap: dict):
        """Send a MeterValues message."""
        ts = datetime.now(timezone.utc).isoformat()
        sampled_values = [
            {
                "value": str(snap["energy_wh"]),
                "measurand": Measurand.energy_active_import_register,
                "unit": UnitOfMeasure.wh,
            },
            {
                "value": str(snap["power_w"]),
                "measurand": Measurand.power_active_import,
                "unit": UnitOfMeasure.w,
            },
            {
                "value": str(snap["current_a"]),
                "measurand": Measurand.current_import,
                "unit": UnitOfMeasure.a,
            },
            {
                "value": str(snap["soc"]),
                "measurand": Measurand.soc,
                "unit": UnitOfMeasure.percent,
            },
            {
                "value": str(snap["voltage"]),
                "measurand": Measurand.voltage,
                "unit": UnitOfMeasure.v,
            },
        ]

        await self.call(
            call.MeterValuesPayload(
                connector_id=connector_id,
                transaction_id=transaction_id,
                meter_value=[{"timestamp": ts, "sampled_value": sampled_values}],
            )
        )

    async def _stop_charging(self, connector_id: int, reason: str = "Remote"):
        """Stop a charging session."""
        session = self.sessions.get(connector_id)
        if not session or not session.active:
            log.warning("No active session on connector %d to stop", connector_id)
            return

        session.active = False

        # Cancel meter task
        task = self._meter_tasks.pop(connector_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._finish_stop(session, reason)

    async def _finish_stop(self, session: ChargingSession, reason: str = "Remote"):
        """Finalize the stop: send StopTransaction, update status."""
        final_meter = int(session.meter_wh)
        self.cumulative_meter[session.connector_id] = session.meter_wh

        await self.call(
            call.StopTransactionPayload(
                meter_stop=final_meter,
                timestamp=datetime.now(timezone.utc).isoformat(),
                transaction_id=session.transaction_id,
                reason=reason,
            )
        )
        log.info(
            "StopTransaction connector=%d txn=%d meter_stop=%d reason=%s",
            session.connector_id, session.transaction_id, final_meter, reason,
        )

        del self.sessions[session.connector_id]

        # Finishing → Available
        await self.send_status(session.connector_id, ChargePointStatus.finishing)
        await asyncio.sleep(5)
        await self.send_status(session.connector_id, ChargePointStatus.available)

    # ─── OCPP message handlers ───────────────────────────────────────────

    @on(Action.RemoteStartTransaction)
    async def on_remote_start(self, id_tag: str, connector_id: int = 1, **kwargs):
        log.info("RemoteStartTransaction connector=%d id_tag=%s", connector_id, id_tag)
        if connector_id in self.sessions:
            return call_result.RemoteStartTransactionPayload(
                status=RemoteStartStopStatus.rejected
            )
        asyncio.create_task(self._start_charging(connector_id, id_tag))
        return call_result.RemoteStartTransactionPayload(
            status=RemoteStartStopStatus.accepted
        )

    @on(Action.RemoteStopTransaction)
    async def on_remote_stop(self, transaction_id: int, **kwargs):
        log.info("RemoteStopTransaction txn=%d", transaction_id)
        for conn_id, session in self.sessions.items():
            if session.transaction_id == transaction_id:
                asyncio.create_task(self._stop_charging(conn_id, "Remote"))
                return call_result.RemoteStopTransactionPayload(
                    status=RemoteStartStopStatus.accepted
                )
        return call_result.RemoteStopTransactionPayload(
            status=RemoteStartStopStatus.rejected
        )

    @on(Action.GetConfiguration)
    async def on_get_configuration(self, key: list = None, **kwargs):
        log.info("GetConfiguration key=%s", key)
        entries = []
        unknown = []
        keys_to_return = key if key else list(self._config.keys())
        for k in keys_to_return:
            if k in self._config:
                entries.append({"key": k, "readonly": True, "value": self._config[k]})
            else:
                unknown.append(k)
        return call_result.GetConfigurationPayload(
            configuration_key=entries, unknown_key=unknown
        )

    @on(Action.ChangeConfiguration)
    async def on_change_configuration(self, key: str, value: str, **kwargs):
        log.info("ChangeConfiguration %s=%s", key, value)
        if key in self._config:
            self._config[key] = value
            return call_result.ChangeConfigurationPayload(status="Accepted")
        return call_result.ChangeConfigurationPayload(status="NotSupported")

    @on(Action.Reset)
    async def on_reset(self, type: str, **kwargs):
        log.info("Reset type=%s", type)
        # Stop all sessions
        for conn_id in list(self.sessions.keys()):
            await self._stop_charging(conn_id, "Reboot")
        return call_result.ResetPayload(status=ResetStatus.accepted)

    @on(Action.TriggerMessage)
    async def on_trigger_message(self, requested_message: str, connector_id: int = 0, **kwargs):
        log.info("TriggerMessage %s connector=%d", requested_message, connector_id)
        if requested_message == "StatusNotification":
            status = self.connector_status.get(connector_id, ChargePointStatus.available)
            asyncio.create_task(self.send_status(connector_id, status))
            return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "Heartbeat":
            asyncio.create_task(self.call(call.HeartbeatPayload()))
            return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "MeterValues":
            session = self.sessions.get(connector_id)
            if session:
                snap = session.tick(0, MAX_KW)
                asyncio.create_task(
                    self._send_meter_values(connector_id, session.transaction_id, snap)
                )
                return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "BootNotification":
            asyncio.create_task(
                self.call(
                    call.BootNotificationPayload(
                        charge_point_vendor=VENDOR,
                        charge_point_model=MODEL,
                        charge_point_serial_number=SERIAL,
                        firmware_version=FIRMWARE,
                    )
                )
            )
            return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        return call_result.TriggerMessagePayload(status=TriggerMessageStatus.not_implemented)

    async def cleanup(self):
        """Clean shutdown."""
        log.info("Shutting down...")
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        for task in self._meter_tasks.values():
            task.cancel()
        for conn_id in list(self.sessions.keys()):
            try:
                await self._stop_charging(conn_id, "PowerLoss")
            except Exception:
                pass


# ─── Main loop with reconnect ────────────────────────────────────────────────

shutdown_event = asyncio.Event()


def handle_signal(*_):
    log.info("Signal received, shutting down")
    shutdown_event.set()


async def connect_and_run():
    """Connect to OCPP server and run until disconnected or shutdown."""
    url = OCPP_URL.replace("{cp_id}", CP_ID)
    log.info("Connecting to %s as %s", url, CP_ID)

    async with websockets.connect(
        url,
        subprotocols=["ocpp1.6"],
        ping_interval=20,
        ping_timeout=30,
        close_timeout=10,
    ) as ws:
        charger = VirtualCharger(CP_ID, ws)
        log.info("Connected to %s", url)

        # Run boot + message handler concurrently
        boot_task = asyncio.create_task(charger.boot())
        handler_task = asyncio.create_task(charger.start())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        # Wait for boot to complete first (don't treat boot finishing as exit)
        try:
            await boot_task
        except Exception as e:
            log.error("Boot failed: %s", e)

        # Now wait for either handler (connection lost) or shutdown
        done, pending = await asyncio.wait(
            [handler_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        await charger.cleanup()

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def main():
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    delay = RECONNECT_DELAY
    max_delay = 60

    while not shutdown_event.is_set():
        try:
            await connect_and_run()
            if shutdown_event.is_set():
                break
            delay = RECONNECT_DELAY  # Reset on clean disconnect
        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidURI,
            websockets.exceptions.InvalidHandshake,
            ConnectionRefusedError,
            OSError,
        ) as e:
            log.warning("Connection lost: %s", e)
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)

        if not shutdown_event.is_set():
            log.info("Reconnecting in %ds...", delay)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, max_delay)

    log.info("Charger %s stopped", CP_ID)


if __name__ == "__main__":
    asyncio.run(main())
