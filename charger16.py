"""
OCPP 1.6j Virtual Charger — fully integrated with all simulation modules.
Each instance is lightweight async, designed for 100+ per event loop.
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional

import websockets
from ocpp.v16 import ChargePoint as CP16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ChargePointStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    Measurand,
    RegistrationStatus,
    RemoteStartStopStatus,
    ReservationStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UnitOfMeasure,
    UpdateStatus,
)
from ocpp.routing import on

from profiles import ChargerProfile, QuirkConfig, OcppVersion
from physics import (
    ChargeState,
    ChargingProfileLimit,
    EnvironmentConditions,
    MeterSnapshot,
    PowerDirection,
    V2GConfig,
    random_start_soc,
)
from metrics import farm_metrics
from network import NetworkLayer, ConnectionState, OfflineBuffer
from environment import EnvironmentSimulator, ActiveFault, FaultType
from pnc import PnCConfig, PnCSession, generate_emaid, generate_exi_cert_request

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectorState:
    """State for a single connector."""

    def __init__(self, connector_id: int):
        self.connector_id = connector_id
        self.status: str = ChargePointStatus.available
        self.transaction_id: Optional[int] = None
        self.id_tag: Optional[str] = None
        self.charge_state: Optional[ChargeState] = None
        self.meter_task: Optional[asyncio.Task] = None
        self.reservation_id: Optional[int] = None
        self.reserved_id_tag: Optional[str] = None
        self.available: bool = True  # ChangeAvailability


class VirtualCharger16:
    """
    OCPP 1.6j virtual charger instance.
    Integrates profiles, physics, network, environment, PnC, and metrics.
    """

    def __init__(
        self,
        cp_id: str,
        profile: ChargerProfile,
        ws_url: str,
        quirks: Optional[QuirkConfig] = None,
        site_id: str = "default",
        env_sim: Optional[EnvironmentSimulator] = None,
        pnc_config: Optional[PnCConfig] = None,
    ):
        self.cp_id = cp_id
        self.profile = profile
        self.ws_url = ws_url
        self.quirks = quirks or profile.default_quirks
        self.site_id = site_id
        self.env_sim = env_sim or EnvironmentSimulator()
        self.pnc_config = pnc_config or PnCConfig()

        # Network layer
        self.network = NetworkLayer(cp_id)

        # Connector states
        self.connectors: dict[int, ConnectorState] = {}
        for i in range(1, profile.num_connectors + 1):
            self.connectors[i] = ConnectorState(i)

        # OCPP configuration keys
        self._config: dict[str, dict] = {
            "HeartbeatInterval": {"value": "30", "readonly": False},
            "MeterValueSampleInterval": {"value": "30", "readonly": False},
            "NumberOfConnectors": {"value": str(profile.num_connectors), "readonly": True},
            "ChargePointVendor": {"value": profile.vendor, "readonly": True},
            "ChargePointModel": {"value": profile.model, "readonly": True},
            "FirmwareVersion": {"value": profile.firmware, "readonly": True},
            "WebSocketPingInterval": {"value": "0" if self.quirks.websocket_ping_interval_zero else "30", "readonly": False},
            "ConnectionTimeOut": {"value": "180", "readonly": False},
            "MeterValuesSampledData": {"value": "Energy.Active.Import.Register,Power.Active.Import,Current.Import,SoC,Voltage", "readonly": False},
            "StopTransactionOnInvalidId": {"value": "false", "readonly": False},
            "AuthorizeRemoteTxRequests": {"value": "true", "readonly": False},
            "LocalAuthListEnabled": {"value": "false", "readonly": False},
            "SupportedFeatureProfiles": {"value": "Core,FirmwareManagement,SmartCharging,RemoteTrigger,LocalAuthListManagement,Reservation", "readonly": True},
        }

        # Local auth list
        self._local_auth_list: dict[str, str] = {}  # id_tag -> status
        self._local_auth_version: int = 0

        # Charging profiles (stack_level -> profile)
        self._charging_profiles: dict[int, dict] = {}

        # Firmware update state
        self._firmware_status: Optional[str] = None
        self._firmware_task: Optional[asyncio.Task] = None

        # Diagnostics state
        self._diagnostics_status: Optional[str] = None

        # Internal tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cp: Optional[CP16] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._connected = False
        self._reconnect_after_stop = False

        # Cumulative meters (persist across sessions)
        self._cumulative_wh: dict[int, float] = {i: random.uniform(0, 50000) for i in range(1, profile.num_connectors + 1)}
        self._cumulative_export_wh: dict[int, float] = {i: 0.0 for i in range(1, profile.num_connectors + 1)}

        # Transaction ID counter (server assigns, but we track)
        self._next_local_txn_id = 1

    @property
    def is_connected(self) -> bool:
        return self._connected and self.network.state == ConnectionState.CONNECTED

    @property
    def status_summary(self) -> dict:
        connectors = {}
        for cid, conn in self.connectors.items():
            connectors[cid] = {
                "status": conn.status,
                "transaction_id": conn.transaction_id,
                "soc": round(conn.charge_state.soc, 1) if conn.charge_state else None,
                "power_kw": round(conn.charge_state.current_power_kw, 2) if conn.charge_state else 0,
                "energy_wh": round(conn.charge_state.meter_wh, 0) if conn.charge_state else 0,
                "direction": conn.charge_state.direction.value if conn.charge_state else "idle",
                "available": conn.available,
                "reserved": conn.reservation_id is not None,
            }
        return {
            "cp_id": self.cp_id,
            "profile": self.profile.name,
            "ocpp_version": "1.6",
            "connected": self.is_connected,
            "running": self._running,
            "site_id": self.site_id,
            "quirks": self.quirks.as_dict(),
            "connectors": connectors,
            "network": self.network.status(),
            "firmware_status": self._firmware_status,
        }

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the charger's connection loop."""
        self._running = True
        self._shutdown_event.clear()
        farm_metrics.log_event("info", self.cp_id, f"Charger starting (profile={self.profile.name})")

        delay = 5
        max_delay = 60

        while self._running and not self._shutdown_event.is_set():
            try:
                await self._connect_and_run()
                if self._shutdown_event.is_set():
                    break
                delay = 5
            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.InvalidURI,
                websockets.exceptions.InvalidHandshake,
                ConnectionRefusedError,
                OSError,
            ) as e:
                farm_metrics.record_disconnection()
                farm_metrics.record_error()
                farm_metrics.log_event("warning", self.cp_id, f"Connection lost: {e}")
                self._connected = False
                self.network.go_offline()
            except Exception as e:
                farm_metrics.record_error()
                farm_metrics.log_event("error", self.cp_id, f"Unexpected error: {e}")
                self._connected = False

            if self._running and not self._shutdown_event.is_set():
                farm_metrics.log_event("info", self.cp_id, f"Reconnecting in {delay}s...")
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, max_delay)

        self._running = False
        farm_metrics.log_event("info", self.cp_id, "Charger stopped")

    async def stop(self) -> None:
        """Gracefully stop the charger."""
        self._running = False
        self._shutdown_event.set()
        await self._cleanup()

    async def _connect_and_run(self) -> None:
        """Single connection attempt."""
        url = self.ws_url.rstrip("/") + f"/{self.cp_id}"
        ping_interval = 20
        if self.quirks.websocket_ping_interval_zero:
            ping_interval = None  # Server must ping us

        async with websockets.connect(
            url,
            subprotocols=["ocpp1.6"],
            ping_interval=ping_interval,
            ping_timeout=30,
            close_timeout=10,
            max_size=2**20,
        ) as ws:
            self._ws = ws
            self._cp = _ChargePointHandler(self.cp_id, ws, self)
            self._connected = True
            self.network.go_online()
            farm_metrics.record_connection()
            farm_metrics.log_event("info", self.cp_id, "Connected")

            # Replay offline buffer
            if self.quirks.stop_transaction_on_reconnect:
                await self._replay_reconnect_stops()

            await self._replay_offline_buffer()

            # Boot sequence
            await self._boot()

            # Run message handler
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            handler_task = asyncio.create_task(self._cp.start())

            done, pending = await asyncio.wait(
                [handler_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            await self._cleanup()
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _boot(self) -> None:
        """Run OCPP boot sequence."""
        t0 = time.monotonic()
        resp = await self._call(
            call.BootNotificationPayload(
                charge_point_vendor=self.profile.vendor,
                charge_point_model=self.profile.model,
                charge_point_serial_number=f"{self.cp_id}-SN",
                firmware_version=self.profile.firmware,
            )
        )
        farm_metrics.record_latency((time.monotonic() - t0) * 1000)

        if resp and resp.status == RegistrationStatus.accepted:
            if resp.interval > 0:
                self._config["HeartbeatInterval"]["value"] = str(resp.interval)
            farm_metrics.log_event("info", self.cp_id, f"Boot accepted (interval={resp.interval}s)")
        else:
            farm_metrics.log_event("warning", self.cp_id, f"Boot status: {resp.status if resp else 'no response'}")

        # StatusNotification for charger (connector 0) + each connector
        for conn_id in range(0, self.profile.num_connectors + 1):
            status = ChargePointStatus.available
            if conn_id > 0 and not self.connectors[conn_id].available:
                status = ChargePointStatus.unavailable
            await self._send_status(conn_id, status)

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _cleanup(self) -> None:
        """Cancel all internal tasks."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for conn in self.connectors.values():
            if conn.meter_task:
                conn.meter_task.cancel()
                conn.meter_task = None
        if self._firmware_task:
            self._firmware_task.cancel()
            self._firmware_task = None
        self._connected = False

    # ─── OCPP message sending ────────────────────────────────────────────

    async def _call(self, payload):
        """Send an OCPP call through the network layer."""
        if not self._cp or not self._connected:
            return None
        try:
            farm_metrics.record_message_sent()
            t0 = time.monotonic()
            result = await self._cp.call(payload)
            farm_metrics.record_message_received()
            farm_metrics.record_latency((time.monotonic() - t0) * 1000)
            return result
        except Exception as e:
            farm_metrics.record_error()
            log.debug("[%s] Call failed: %s", self.cp_id, e)
            return None

    async def _send_status(self, connector_id: int, status: str) -> None:
        """Send StatusNotification and update internal state."""
        if connector_id > 0 and connector_id in self.connectors:
            self.connectors[connector_id].status = status
        await self._call(
            call.StatusNotificationPayload(
                connector_id=connector_id,
                error_code=ChargePointErrorCode.no_error,
                status=status,
            )
        )

    async def _send_status_with_error(self, connector_id: int, status: str, error_code: str, info: str = "") -> None:
        """Send StatusNotification with error code."""
        if connector_id > 0 and connector_id in self.connectors:
            self.connectors[connector_id].status = status
        await self._call(
            call.StatusNotificationPayload(
                connector_id=connector_id,
                error_code=error_code,
                status=status,
                info=info,
            )
        )

    # ─── Heartbeat ───────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        try:
            while self._connected:
                interval = int(self._config["HeartbeatInterval"]["value"])
                await asyncio.sleep(interval)
                if not self._connected:
                    break
                t0 = time.monotonic()
                resp = await self._call(call.HeartbeatPayload())
                if resp:
                    farm_metrics.record_latency((time.monotonic() - t0) * 1000)
        except asyncio.CancelledError:
            pass

    # ─── Charging sessions ───────────────────────────────────────────────

    async def start_charging(self, connector_id: int = 1, id_tag: str = "VIRTUAL-TAG") -> bool:
        """Externally triggered charge start."""
        conn = self.connectors.get(connector_id)
        if not conn or conn.transaction_id is not None:
            return False
        if not conn.available:
            return False
        asyncio.create_task(self._do_start_charging(connector_id, id_tag))
        return True

    async def _do_authorize_then_start(self, connector_id: int, id_tag: str) -> None:
        """Maxpower behavior: send Authorize AFTER accepting RemoteStart, BEFORE StartTransaction."""
        try:
            result = await self._call(call.AuthorizePayload(id_tag=id_tag))
            status = result.id_tag_info.get("status", "Accepted") if hasattr(result, "id_tag_info") else "Accepted"
            if status != "Accepted":
                logger.warning(f"[{self.cp_id}] Authorize rejected for {id_tag}: {status}")
                return
        except Exception as e:
            logger.warning(f"[{self.cp_id}] Authorize failed: {e}")
            # Continue anyway — charger starts even if authorize call fails
        await self._do_start_charging(connector_id, id_tag)

    async def _do_start_charging(self, connector_id: int, id_tag: str) -> None:
        conn = self.connectors[connector_id]

        # Check reservation
        if conn.reservation_id is not None and conn.reserved_id_tag != id_tag:
            farm_metrics.log_event("warning", self.cp_id, f"Connector {connector_id} reserved for {conn.reserved_id_tag}")
            return

        # Preparing
        await self._send_status(connector_id, ChargePointStatus.preparing)
        await asyncio.sleep(random.uniform(1, 3))

        # Check for PnC
        if self.pnc_config.enabled and id_tag.startswith("EMAID:"):
            id_tag = id_tag.replace("EMAID:", "")

        # Start SoC
        soc = random_start_soc()
        meter_start = int(self._cumulative_wh.get(connector_id, 0))

        # StartTransaction
        resp = await self._call(
            call.StartTransactionPayload(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=meter_start,
                timestamp=_now_iso(),
            )
        )
        if not resp:
            await self._send_status(connector_id, ChargePointStatus.available)
            return

        txn_id = resp.transaction_id
        conn.transaction_id = txn_id
        conn.id_tag = id_tag

        # Init physics
        env_conds = EnvironmentConditions(ambient_temp_c=self.env_sim.site.ambient_temp_c)
        conn.charge_state = ChargeState(
            soc=soc,
            meter_wh=float(meter_start),
            max_kw=self.profile.max_kw,
        )
        conn.charge_state.env = env_conds
        conn.charge_state.direction = PowerDirection.CHARGING

        # Apply any active charging profiles
        self._apply_charging_profiles(conn)

        # Clear reservation if any
        conn.reservation_id = None
        conn.reserved_id_tag = None

        farm_metrics.record_session_started()
        farm_metrics.log_event("info", self.cp_id, f"Started txn={txn_id} conn={connector_id} soc={soc:.1f}%")

        await self._send_status(connector_id, ChargePointStatus.charging)

        # Start meter loop
        conn.meter_task = asyncio.create_task(self._meter_loop(connector_id))

    async def stop_charging(self, connector_id: int = 1, reason: str = "Remote") -> bool:
        """Externally triggered charge stop."""
        conn = self.connectors.get(connector_id)
        if not conn or conn.transaction_id is None:
            return False
        asyncio.create_task(self._do_stop_charging(connector_id, reason))
        return True

    async def _do_stop_charging(self, connector_id: int, reason: str = "Remote") -> None:
        conn = self.connectors[connector_id]
        if conn.transaction_id is None:
            return

        # Cancel meter task
        if conn.meter_task:
            conn.meter_task.cancel()
            try:
                await conn.meter_task
            except asyncio.CancelledError:
                pass
            conn.meter_task = None

        # Final meter
        meter_stop = int(conn.charge_state.meter_wh) if conn.charge_state else int(self._cumulative_wh.get(connector_id, 0))
        self._cumulative_wh[connector_id] = float(meter_stop)
        if conn.charge_state:
            self._cumulative_export_wh[connector_id] = conn.charge_state.discharge_meter_wh

        # StopTransaction
        if self._connected:
            await self._call(
                call.StopTransactionPayload(
                    meter_stop=meter_stop,
                    timestamp=_now_iso(),
                    transaction_id=conn.transaction_id,
                    reason=reason,
                )
            )
        else:
            # Queue for offline replay
            self.network.offline_buffer.queue_meter_value({
                "type": "StopTransaction",
                "meter_stop": meter_stop,
                "timestamp": _now_iso(),
                "transaction_id": conn.transaction_id,
                "reason": reason,
            })

        farm_metrics.record_session_ended()
        farm_metrics.log_event("info", self.cp_id, f"Stopped txn={conn.transaction_id} conn={connector_id} reason={reason}")

        conn.transaction_id = None
        conn.id_tag = None
        conn.charge_state = None

        # Finishing -> Available
        await self._send_status(connector_id, ChargePointStatus.finishing)
        await asyncio.sleep(random.uniform(2, 5))
        status = ChargePointStatus.available if conn.available else ChargePointStatus.unavailable
        await self._send_status(connector_id, status)

    # ─── V2G discharge ───────────────────────────────────────────────────

    async def start_v2g(self, connector_id: int = 1, max_discharge_kw: float = 50.0, min_soc: float = 20.0) -> bool:
        """Start V2G discharge on an active session."""
        conn = self.connectors.get(connector_id)
        if not conn or not conn.charge_state or conn.transaction_id is None:
            return False
        conn.charge_state.v2g = V2GConfig(enabled=True, max_discharge_kw=max_discharge_kw, min_soc_floor=min_soc)
        conn.charge_state.direction = PowerDirection.DISCHARGING
        farm_metrics.log_event("info", self.cp_id, f"V2G started conn={connector_id} max={max_discharge_kw}kW")
        return True

    async def stop_v2g(self, connector_id: int = 1) -> bool:
        conn = self.connectors.get(connector_id)
        if not conn or not conn.charge_state:
            return False
        conn.charge_state.v2g.enabled = False
        conn.charge_state.direction = PowerDirection.CHARGING
        farm_metrics.log_event("info", self.cp_id, f"V2G stopped conn={connector_id}")
        return True

    # ─── Meter values loop ───────────────────────────────────────────────

    async def _meter_loop(self, connector_id: int) -> None:
        conn = self.connectors[connector_id]
        try:
            while conn.transaction_id is not None and conn.charge_state is not None:
                interval = int(self._config["MeterValueSampleInterval"]["value"])
                await asyncio.sleep(interval)

                if conn.transaction_id is None or conn.charge_state is None:
                    break

                # Check environment faults
                fault = self.env_sim.has_session_stopping_fault(self.cp_id, connector_id)
                if fault:
                    farm_metrics.log_event("warning", self.cp_id, f"Fault stops session: {fault.fault_type.value}")
                    await self._do_stop_charging(connector_id, "Other")
                    return

                # Apply environmental derating
                conn.charge_state.hardware_derating = self.env_sim.get_derating_factor(self.cp_id)
                conn.charge_state.env.ambient_temp_c = self.env_sim.site.ambient_temp_c

                # Physics tick
                snap = conn.charge_state.tick(float(interval))
                self._cumulative_wh[connector_id] = conn.charge_state.meter_wh

                # Send or buffer meter values
                if self._connected:
                    await self._send_meter_values(connector_id, conn.transaction_id, snap)
                else:
                    self.network.offline_buffer.queue_meter_value({
                        "type": "MeterValues",
                        "connector_id": connector_id,
                        "transaction_id": conn.transaction_id,
                        "timestamp": _now_iso(),
                        "snapshot": snap.as_dict(),
                    })

                # Check full
                if conn.charge_state.is_full:
                    farm_metrics.log_event("info", self.cp_id, f"SoC 100% conn={connector_id}")
                    await self._do_stop_charging(connector_id, "Local")
                    return

                # Check V2G floor
                if conn.charge_state.direction == PowerDirection.IDLE:
                    farm_metrics.log_event("info", self.cp_id, f"V2G SoC floor reached conn={connector_id}")

        except asyncio.CancelledError:
            pass

    async def _send_meter_values(self, connector_id: int, transaction_id: int, snap: MeterSnapshot) -> None:
        ts = _now_iso()

        def _val(v, as_str: bool) -> str:
            return str(v) if as_str else str(v)

        as_str = self.quirks.meter_values_as_strings

        sampled = [
            {"value": _val(snap.energy_wh, as_str), "measurand": Measurand.energy_active_import_register, "unit": UnitOfMeasure.wh},
        ]
        # Maxpower quirk: does NOT send Power.Active.Import — server must calculate from V×I
        if not self.quirks.no_power_measurand:
            sampled.append({"value": _val(snap.power_w, as_str), "measurand": Measurand.power_active_import, "unit": UnitOfMeasure.w})
        sampled.extend([
            {"value": _val(snap.current_a, as_str), "measurand": Measurand.current_import, "unit": UnitOfMeasure.a},
            {"value": _val(snap.soc, as_str), "measurand": Measurand.soc, "unit": UnitOfMeasure.percent},
            {"value": _val(snap.voltage, as_str), "measurand": Measurand.voltage, "unit": UnitOfMeasure.v},
        ])

        # V2G: add export measurands
        if snap.power_export_w > 0:
            sampled.extend([
                {"value": _val(snap.energy_export_wh, as_str), "measurand": Measurand.energy_active_export_register, "unit": UnitOfMeasure.wh},
                {"value": _val(snap.power_export_w, as_str), "measurand": Measurand.power_active_export, "unit": UnitOfMeasure.w},
            ])

        await self._call(
            call.MeterValuesPayload(
                connector_id=connector_id,
                transaction_id=transaction_id,
                meter_value=[{"timestamp": ts, "sampled_value": sampled}],
            )
        )

    # ─── Offline buffer replay ───────────────────────────────────────────

    async def _replay_offline_buffer(self) -> None:
        queued = self.network.offline_buffer.drain_meter_values()
        if not queued:
            return
        farm_metrics.log_event("info", self.cp_id, f"Replaying {len(queued)} offline messages")
        for msg in queued:
            if msg["type"] == "MeterValues":
                snap_dict = msg["snapshot"]
                sampled = [
                    {"value": str(snap_dict["energy_wh"]), "measurand": Measurand.energy_active_import_register, "unit": UnitOfMeasure.wh},
                    {"value": str(snap_dict["power_w"]), "measurand": Measurand.power_active_import, "unit": UnitOfMeasure.w},
                    {"value": str(snap_dict["soc"]), "measurand": Measurand.soc, "unit": UnitOfMeasure.percent},
                ]
                await self._call(
                    call.MeterValuesPayload(
                        connector_id=msg["connector_id"],
                        transaction_id=msg["transaction_id"],
                        meter_value=[{"timestamp": msg["timestamp"], "sampled_value": sampled}],
                    )
                )
            elif msg["type"] == "StopTransaction":
                await self._call(
                    call.StopTransactionPayload(
                        meter_stop=msg["meter_stop"],
                        timestamp=msg["timestamp"],
                        transaction_id=msg["transaction_id"],
                        reason=msg["reason"],
                    )
                )
            await asyncio.sleep(0.1)  # Don't flood

    async def _replay_reconnect_stops(self) -> None:
        """MAXPOWER quirk: send StopTransaction reason=Other for sessions active during disconnect."""
        for conn in self.connectors.values():
            if conn.transaction_id is not None:
                meter = int(conn.charge_state.meter_wh) if conn.charge_state else 0
                await self._call(
                    call.StopTransactionPayload(
                        meter_stop=meter,
                        timestamp=_now_iso(),
                        transaction_id=conn.transaction_id,
                        reason="Other",
                    )
                )
                conn.transaction_id = None
                conn.charge_state = None
                farm_metrics.log_event("info", self.cp_id, f"Reconnect StopTransaction (quirk) conn={conn.connector_id}")

    # ─── Charging profiles ───────────────────────────────────────────────

    def _apply_charging_profiles(self, conn: ConnectorState) -> None:
        """Apply stored charging profiles to a connector's charge state."""
        if conn.charge_state is None:
            return
        conn.charge_state.charging_profiles.clear()
        for sl, prof in self._charging_profiles.items():
            if prof.get("connector_id", 0) in (0, conn.connector_id):
                schedule = prof.get("charging_schedule", {})
                periods = schedule.get("charging_schedule_period", [])
                if periods:
                    for period in periods:
                        conn.charge_state.charging_profiles.append(
                            ChargingProfileLimit(
                                limit_kw=period.get("limit", self.profile.max_kw),
                                stack_level=prof.get("stack_level", 0),
                                start_time=period.get("start_period", 0),
                            )
                        )

    # ─── PnC flow ────────────────────────────────────────────────────────

    async def trigger_pnc(self, connector_id: int = 1) -> bool:
        """Trigger Plug & Charge flow (1.6j: DataTransfer + eMAID id_tag)."""
        if not self.pnc_config.enabled:
            return False

        self.pnc_config.contract_id_counter += 1
        emaid = generate_emaid(self.pnc_config.emaid_prefix, self.pnc_config.contract_id_counter)
        exi = generate_exi_cert_request(emaid)

        # Simulate TLS handshake delay
        await asyncio.sleep(self.pnc_config.tls_handshake_delay_sec)

        # DataTransfer with ISO 15118 cert data
        await self._call(
            call.DataTransferPayload(
                vendor_id="org.openchargealliance.iso15118pnc",
                message_id="Authorize",
                data=json.dumps({"eMAID": emaid, "exiRequest": exi}),
            )
        )

        # Start charging with eMAID as id_tag
        await self.start_charging(connector_id, f"EMAID:{emaid}")
        farm_metrics.log_event("info", self.cp_id, f"PnC flow started emaid={emaid}")
        return True

    # ─── Error injection ─────────────────────────────────────────────────

    async def inject_error(self, error_code: str, connector_id: int = 1) -> None:
        """Inject an OCPP error on a connector."""
        await self._send_status_with_error(
            connector_id,
            ChargePointStatus.faulted,
            error_code,
            f"Injected error: {error_code}",
        )
        farm_metrics.log_event("warning", self.cp_id, f"Error injected: {error_code} conn={connector_id}")

    # ─── Force disconnect / reconnect ────────────────────────────────────

    async def force_disconnect(self) -> None:
        """Force WebSocket disconnect."""
        self._connected = False
        self.network.go_offline()
        if self._ws:
            await self._ws.close()
        farm_metrics.log_event("warning", self.cp_id, "Forced disconnect")

    async def force_reconnect(self) -> None:
        """Force reconnect (disconnect + let reconnect loop handle it)."""
        await self.force_disconnect()
        # The reconnect loop in start() will handle reconnection

    # ─── Firmware simulation ─────────────────────────────────────────────

    async def _simulate_firmware_update(self, location: str, retrieve_date: str) -> None:
        """Simulate firmware update lifecycle."""
        try:
            await self._send_firmware_status(FirmwareStatus.downloading)
            await asyncio.sleep(random.uniform(5, 15))
            await self._send_firmware_status(FirmwareStatus.downloaded)
            await asyncio.sleep(2)
            await self._send_firmware_status(FirmwareStatus.installing)
            await asyncio.sleep(random.uniform(10, 30))

            # 90% success rate
            if random.random() < 0.9:
                await self._send_firmware_status(FirmwareStatus.installed)
                farm_metrics.log_event("info", self.cp_id, "Firmware update installed")
            else:
                await self._send_firmware_status(FirmwareStatus.installation_failed)
                farm_metrics.log_event("error", self.cp_id, "Firmware update failed")
        except asyncio.CancelledError:
            pass

    async def _send_firmware_status(self, status: str) -> None:
        self._firmware_status = status
        await self._call(
            call.FirmwareStatusNotificationPayload(status=status)
        )


# ─── OCPP 1.6j Message Handler ──────────────────────────────────────────────


class _ChargePointHandler(CP16):
    """Internal OCPP 1.6j handler that delegates to VirtualCharger16."""

    def __init__(self, cp_id: str, ws, charger: VirtualCharger16):
        super().__init__(cp_id, ws)
        self.charger = charger

    @on(Action.RemoteStartTransaction)
    async def on_remote_start(self, id_tag: str, connector_id: int = 1, **kwargs):
        farm_metrics.record_message_received()
        conn = self.charger.connectors.get(connector_id)
        if not conn or conn.transaction_id is not None:
            return call_result.RemoteStartTransactionPayload(status=RemoteStartStopStatus.rejected)

        # Maxpower quirk: accepts RemoteStart even without car, but silently ignores
        # Only actually starts if connector is in Preparing state (car connected)
        if self.quirks.remote_start_needs_cable and conn.available:
            # Connector is Available (no car) — accept but don't start
            logger.info(f"[{self.charger.cp_id}] RemoteStart accepted but no car on connector {connector_id} — silent ignore")
            return call_result.RemoteStartTransactionPayload(status=RemoteStartStopStatus.accepted)

        # Check charging profile in kwargs
        if "charging_profile" in kwargs and kwargs["charging_profile"]:
            prof = kwargs["charging_profile"]
            sl = prof.get("stack_level", 0)
            self.charger._charging_profiles[sl] = prof

        # Maxpower quirk: sends Authorize before StartTransaction
        if self.quirks.authorize_before_start:
            asyncio.create_task(self.charger._do_authorize_then_start(connector_id, id_tag))
        else:
            asyncio.create_task(self.charger._do_start_charging(connector_id, id_tag))
        return call_result.RemoteStartTransactionPayload(status=RemoteStartStopStatus.accepted)

    @on(Action.RemoteStopTransaction)
    async def on_remote_stop(self, transaction_id: int, **kwargs):
        farm_metrics.record_message_received()
        for conn in self.charger.connectors.values():
            if conn.transaction_id == transaction_id:
                asyncio.create_task(self.charger._do_stop_charging(conn.connector_id, "Remote"))
                return call_result.RemoteStopTransactionPayload(status=RemoteStartStopStatus.accepted)
        return call_result.RemoteStopTransactionPayload(status=RemoteStartStopStatus.rejected)

    @on(Action.GetConfiguration)
    async def on_get_config(self, key: list = None, **kwargs):
        farm_metrics.record_message_received()
        entries = []
        unknown = []
        keys = key if key else list(self.charger._config.keys())
        for k in keys:
            if k in self.charger._config:
                cfg = self.charger._config[k]
                entries.append({"key": k, "readonly": cfg["readonly"], "value": cfg["value"]})
            else:
                unknown.append(k)
        return call_result.GetConfigurationPayload(configuration_key=entries, unknown_key=unknown)

    @on(Action.ChangeConfiguration)
    async def on_change_config(self, key: str, value: str, **kwargs):
        farm_metrics.record_message_received()
        # MAXPOWER quirk: reject ConnectionTimeOut changes
        if key == "ConnectionTimeOut" and self.charger.quirks.reject_connection_timeout_change:
            return call_result.ChangeConfigurationPayload(status=ConfigurationStatus.rejected)

        if key in self.charger._config:
            if self.charger._config[key]["readonly"]:
                return call_result.ChangeConfigurationPayload(status=ConfigurationStatus.rejected)
            self.charger._config[key]["value"] = value
            return call_result.ChangeConfigurationPayload(status=ConfigurationStatus.accepted)
        return call_result.ChangeConfigurationPayload(status=ConfigurationStatus.not_supported)

    @on(Action.SetChargingProfile)
    async def on_set_charging_profile(self, connector_id: int, cs_charging_profiles: dict, **kwargs):
        farm_metrics.record_message_received()
        prof = cs_charging_profiles
        sl = prof.get("stack_level", 0)
        prof["connector_id"] = connector_id
        self.charger._charging_profiles[sl] = prof

        # Apply to active session
        conn = self.charger.connectors.get(connector_id)
        if conn and conn.charge_state:
            self.charger._apply_charging_profiles(conn)

        return call_result.SetChargingProfilePayload(status="Accepted")

    @on(Action.ClearChargingProfile)
    async def on_clear_charging_profile(self, **kwargs):
        farm_metrics.record_message_received()
        profile_id = kwargs.get("id")
        connector_id = kwargs.get("connector_id")
        stack_level = kwargs.get("stack_level")

        if stack_level is not None and stack_level in self.charger._charging_profiles:
            del self.charger._charging_profiles[stack_level]
            return call_result.ClearChargingProfilePayload(status=ClearChargingProfileStatus.accepted)
        elif connector_id is not None:
            to_remove = [sl for sl, p in self.charger._charging_profiles.items() if p.get("connector_id") == connector_id]
            for sl in to_remove:
                del self.charger._charging_profiles[sl]
            if to_remove:
                return call_result.ClearChargingProfilePayload(status=ClearChargingProfileStatus.accepted)
        elif not kwargs or profile_id is None:
            self.charger._charging_profiles.clear()
            return call_result.ClearChargingProfilePayload(status=ClearChargingProfileStatus.accepted)

        return call_result.ClearChargingProfilePayload(status=ClearChargingProfileStatus.unknown)

    @on(Action.GetCompositeSchedule)
    async def on_get_composite_schedule(self, connector_id: int, duration: int, **kwargs):
        farm_metrics.record_message_received()
        # Build composite from active profiles
        periods = []
        for sl, prof in sorted(self.charger._charging_profiles.items()):
            schedule = prof.get("charging_schedule", {})
            for period in schedule.get("charging_schedule_period", []):
                periods.append(period)

        if periods:
            return call_result.GetCompositeSchedulePayload(
                status="Accepted",
                connector_id=connector_id,
                schedule_start=_now_iso(),
                charging_schedule={
                    "duration": duration,
                    "charging_rate_unit": kwargs.get("charging_rate_unit", "W"),
                    "charging_schedule_period": periods,
                },
            )
        return call_result.GetCompositeSchedulePayload(status="Rejected")

    @on(Action.Reset)
    async def on_reset(self, type: str, **kwargs):
        farm_metrics.record_message_received()
        # Stop all sessions
        for conn in self.charger.connectors.values():
            if conn.transaction_id is not None:
                await self.charger._do_stop_charging(conn.connector_id, "Reboot")

        if type == ResetType.hard:
            # Hard reset: simulate brief disconnect
            asyncio.create_task(self._hard_reset())
        return call_result.ResetPayload(status=ResetStatus.accepted)

    async def _hard_reset(self):
        await asyncio.sleep(2)
        await self.charger.force_disconnect()

    @on(Action.TriggerMessage)
    async def on_trigger_message(self, requested_message: str, connector_id: int = 0, **kwargs):
        farm_metrics.record_message_received()
        if requested_message == "StatusNotification":
            conn = self.charger.connectors.get(connector_id)
            status = conn.status if conn else ChargePointStatus.available
            asyncio.create_task(self.charger._send_status(connector_id, status))
            return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "Heartbeat":
            asyncio.create_task(self.charger._call(call.HeartbeatPayload()))
            return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "BootNotification":
            asyncio.create_task(self.charger._call(
                call.BootNotificationPayload(
                    charge_point_vendor=self.charger.profile.vendor,
                    charge_point_model=self.charger.profile.model,
                    charge_point_serial_number=f"{self.charger.cp_id}-SN",
                    firmware_version=self.charger.profile.firmware,
                )
            ))
            return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "MeterValues":
            conn = self.charger.connectors.get(connector_id)
            if conn and conn.charge_state and conn.transaction_id:
                snap = conn.charge_state.tick(0)
                asyncio.create_task(self.charger._send_meter_values(connector_id, conn.transaction_id, snap))
                return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "FirmwareStatusNotification":
            if self.charger._firmware_status:
                asyncio.create_task(self.charger._call(
                    call.FirmwareStatusNotificationPayload(status=self.charger._firmware_status)
                ))
                return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        elif requested_message == "DiagnosticsStatusNotification":
            if self.charger._diagnostics_status:
                asyncio.create_task(self.charger._call(
                    call.DiagnosticsStatusNotificationPayload(status=self.charger._diagnostics_status)
                ))
                return call_result.TriggerMessagePayload(status=TriggerMessageStatus.accepted)
        return call_result.TriggerMessagePayload(status=TriggerMessageStatus.not_implemented)

    @on(Action.ChangeAvailability)
    async def on_change_availability(self, connector_id: int, type: str, **kwargs):
        farm_metrics.record_message_received()
        if connector_id == 0:
            # All connectors
            for conn in self.charger.connectors.values():
                conn.available = (type == AvailabilityType.operative)
                if conn.transaction_id is None:
                    status = ChargePointStatus.available if conn.available else ChargePointStatus.unavailable
                    asyncio.create_task(self.charger._send_status(conn.connector_id, status))
            return call_result.ChangeAvailabilityPayload(status=AvailabilityStatus.accepted)
        conn = self.charger.connectors.get(connector_id)
        if conn:
            conn.available = (type == AvailabilityType.operative)
            if conn.transaction_id is None:
                status = ChargePointStatus.available if conn.available else ChargePointStatus.unavailable
                asyncio.create_task(self.charger._send_status(connector_id, status))
            return call_result.ChangeAvailabilityPayload(
                status=AvailabilityStatus.accepted if conn.transaction_id is None else AvailabilityStatus.scheduled
            )
        return call_result.ChangeAvailabilityPayload(status=AvailabilityStatus.rejected)

    @on(Action.ReserveNow)
    async def on_reserve_now(self, connector_id: int, expiry_date: str, id_tag: str, reservation_id: int, **kwargs):
        farm_metrics.record_message_received()
        conn = self.charger.connectors.get(connector_id)
        if not conn:
            return call_result.ReserveNowPayload(status=ReservationStatus.rejected)
        if conn.transaction_id is not None:
            return call_result.ReserveNowPayload(status=ReservationStatus.occupied)
        if not conn.available:
            return call_result.ReserveNowPayload(status=ReservationStatus.unavailable)
        conn.reservation_id = reservation_id
        conn.reserved_id_tag = id_tag
        asyncio.create_task(self.charger._send_status(connector_id, ChargePointStatus.reserved))
        return call_result.ReserveNowPayload(status=ReservationStatus.accepted)

    @on(Action.CancelReservation)
    async def on_cancel_reservation(self, reservation_id: int, **kwargs):
        farm_metrics.record_message_received()
        for conn in self.charger.connectors.values():
            if conn.reservation_id == reservation_id:
                conn.reservation_id = None
                conn.reserved_id_tag = None
                asyncio.create_task(self.charger._send_status(conn.connector_id, ChargePointStatus.available))
                return call_result.CancelReservationPayload(status="Accepted")
        return call_result.CancelReservationPayload(status="Rejected")

    @on(Action.UpdateFirmware)
    async def on_update_firmware(self, location: str, retrieve_date: str, **kwargs):
        farm_metrics.record_message_received()
        if self.charger._firmware_task:
            self.charger._firmware_task.cancel()
        self.charger._firmware_task = asyncio.create_task(
            self.charger._simulate_firmware_update(location, retrieve_date)
        )
        # No payload for UpdateFirmware response in 1.6
        return call_result.UpdateFirmwarePayload()

    @on(Action.GetDiagnostics)
    async def on_get_diagnostics(self, location: str, **kwargs):
        farm_metrics.record_message_received()
        filename = f"diag-{self.charger.cp_id}-{int(time.time())}.txt"
        asyncio.create_task(self._simulate_diagnostics())
        return call_result.GetDiagnosticsPayload(file_name=filename)

    async def _simulate_diagnostics(self):
        try:
            self.charger._diagnostics_status = DiagnosticsStatus.uploading
            await self.charger._call(
                call.DiagnosticsStatusNotificationPayload(status=DiagnosticsStatus.uploading)
            )
            await asyncio.sleep(random.uniform(3, 8))
            self.charger._diagnostics_status = DiagnosticsStatus.uploaded
            await self.charger._call(
                call.DiagnosticsStatusNotificationPayload(status=DiagnosticsStatus.uploaded)
            )
        except asyncio.CancelledError:
            pass

    @on(Action.SendLocalList)
    async def on_send_local_list(self, list_version: int, update_type: str, **kwargs):
        farm_metrics.record_message_received()
        local_auth = kwargs.get("local_authorization_list", [])
        if update_type == "Full":
            self.charger._local_auth_list.clear()
        for entry in local_auth:
            tag = entry.get("id_tag", "")
            status = entry.get("id_tag_info", {}).get("status", "Accepted")
            self.charger._local_auth_list[tag] = status
        self.charger._local_auth_version = list_version
        return call_result.SendLocalListPayload(status=UpdateStatus.accepted)

    @on(Action.DataTransfer)
    async def on_data_transfer(self, vendor_id: str, **kwargs):
        farm_metrics.record_message_received()
        message_id = kwargs.get("message_id", "")
        data = kwargs.get("data", "")
        farm_metrics.log_event("info", self.charger.cp_id, f"DataTransfer vendor={vendor_id} msg={message_id}")
        return call_result.DataTransferPayload(status=DataTransferStatus.accepted, data="{}")
