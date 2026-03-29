"""
OCPP 2.0.1 Virtual Charger — fully integrated with all simulation modules.
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
from ocpp.v201 import ChargePoint as CP201
from ocpp.v201 import call as call201
from ocpp.v201 import call_result as call_result201
from ocpp.v201.enums import (
    Action,
    AuthorizationStatusType,
    BootReasonType,
    ChargingProfilePurposeType,
    ChargingStateType,
    ClearChargingProfileStatusType,
    ConnectorStatusType,
    DataTransferStatusType,
    FirmwareStatusType,
    GetVariableStatusType,
    IdTokenType,
    OperationalStatusType,
    RegistrationStatusType,
    RequestStartStopStatusType,
    ReservationUpdateStatusType,
    ResetStatusType,
    ResetType,
    SetVariableStatusType,
    TransactionEventType,
    TriggerMessageStatusType,
    TriggerMessageType,
    UpdateFirmwareStatusType,
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
from network import NetworkLayer, ConnectionState
from environment import EnvironmentSimulator, ActiveFault, FaultType
from pnc import (
    PnCConfig,
    PnCSession,
    generate_emaid,
    generate_exi_cert_request,
    generate_contract_cert_response,
)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EVSEState:
    """State for a single EVSE (2.0.1 model: evse has connectors)."""

    def __init__(self, evse_id: int, connector_id: int = 1):
        self.evse_id = evse_id
        self.connector_id = connector_id
        self.status: str = ConnectorStatusType.available
        self.transaction_id: Optional[str] = None
        self.id_token: Optional[dict] = None
        self.charge_state: Optional[ChargeState] = None
        self.meter_task: Optional[asyncio.Task] = None
        self.charging_state: str = "Idle"
        self.seq_no: int = 0
        self.reservation_id: Optional[int] = None
        self.available: bool = True


class VirtualCharger201:
    """
    OCPP 2.0.1 virtual charger instance.
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

        # EVSE states (2.0.1 uses evse_id)
        self.evses: dict[int, EVSEState] = {}
        for i in range(1, profile.num_connectors + 1):
            self.evses[i] = EVSEState(evse_id=i, connector_id=1)

        # Device model variables (component.variable -> value)
        self._variables: dict[str, dict] = {
            "OCPPCommCtrlr.HeartbeatInterval": {"value": "30", "mutability": "ReadWrite"},
            "SampledDataCtrlr.TxUpdatedInterval": {"value": "30", "mutability": "ReadWrite"},
            "ChargingStation.Model": {"value": profile.model, "mutability": "ReadOnly"},
            "ChargingStation.VendorName": {"value": profile.vendor, "mutability": "ReadOnly"},
            "ChargingStation.FirmwareVersion": {"value": profile.firmware, "mutability": "ReadOnly"},
            "ChargingStation.SerialNumber": {"value": f"{cp_id}-SN", "mutability": "ReadOnly"},
            "SecurityCtrlr.SecurityProfile": {"value": "1", "mutability": "ReadWrite"},
        }

        # Charging profiles
        self._charging_profiles: dict[int, dict] = {}

        # Firmware
        self._firmware_status: Optional[str] = None
        self._firmware_task: Optional[asyncio.Task] = None

        # Internal state
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cp: Optional[CP201] = None
        self._ws = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._connected = False

        # Cumulative meters
        self._cumulative_wh: dict[int, float] = {i: random.uniform(0, 50000) for i in range(1, profile.num_connectors + 1)}
        self._cumulative_export_wh: dict[int, float] = {i: 0.0 for i in range(1, profile.num_connectors + 1)}

        # Transaction ID counter
        self._txn_counter = 0

    def _next_txn_id(self) -> str:
        self._txn_counter += 1
        return f"{self.cp_id}-TXN-{self._txn_counter:06d}"

    @property
    def is_connected(self) -> bool:
        return self._connected and self.network.state == ConnectionState.CONNECTED

    @property
    def status_summary(self) -> dict:
        evses = {}
        for eid, evse in self.evses.items():
            evses[eid] = {
                "status": evse.status,
                "transaction_id": evse.transaction_id,
                "soc": round(evse.charge_state.soc, 1) if evse.charge_state else None,
                "power_kw": round(evse.charge_state.current_power_kw, 2) if evse.charge_state else 0,
                "energy_wh": round(evse.charge_state.meter_wh, 0) if evse.charge_state else 0,
                "direction": evse.charge_state.direction.value if evse.charge_state else "idle",
                "charging_state": evse.charging_state,
                "available": evse.available,
                "reserved": evse.reservation_id is not None,
            }
        return {
            "cp_id": self.cp_id,
            "profile": self.profile.name,
            "ocpp_version": "2.0.1",
            "connected": self.is_connected,
            "running": self._running,
            "site_id": self.site_id,
            "quirks": self.quirks.as_dict(),
            "evses": evses,
            "network": self.network.status(),
            "firmware_status": self._firmware_status,
        }

    # ─── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._shutdown_event.clear()
        farm_metrics.log_event("info", self.cp_id, f"Charger 2.0.1 starting (profile={self.profile.name})")

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
        farm_metrics.log_event("info", self.cp_id, "Charger 2.0.1 stopped")

    async def stop(self) -> None:
        self._running = False
        self._shutdown_event.set()
        await self._cleanup()

    async def _connect_and_run(self) -> None:
        url = self.ws_url.rstrip("/") + f"/{self.cp_id}"

        async with websockets.connect(
            url,
            subprotocols=["ocpp2.0.1"],
            ping_interval=20 if not self.quirks.websocket_ping_interval_zero else None,
            ping_timeout=30,
            close_timeout=10,
            max_size=2**20,
        ) as ws:
            self._ws = ws
            self._cp = _ChargePointHandler201(self.cp_id, ws, self)
            self._connected = True
            self.network.go_online()
            farm_metrics.record_connection()
            farm_metrics.log_event("info", self.cp_id, "Connected (2.0.1)")

            # Replay offline buffer
            await self._replay_offline_buffer()

            # Boot
            await self._boot()

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
        t0 = time.monotonic()
        resp = await self._call(
            call201.BootNotificationPayload(
                charging_station={
                    "model": self.profile.model,
                    "vendor_name": self.profile.vendor,
                    "serial_number": f"{self.cp_id}-SN",
                    "firmware_version": self.profile.firmware,
                },
                reason=BootReasonType.power_up,
            )
        )
        farm_metrics.record_latency((time.monotonic() - t0) * 1000)

        if resp and resp.status == RegistrationStatusType.accepted:
            if resp.interval > 0:
                self._variables["OCPPCommCtrlr.HeartbeatInterval"]["value"] = str(resp.interval)
            farm_metrics.log_event("info", self.cp_id, f"Boot 2.0.1 accepted (interval={resp.interval}s)")
        else:
            farm_metrics.log_event("warning", self.cp_id, f"Boot 2.0.1 status: {resp.status if resp else 'no response'}")

        # StatusNotification for each EVSE/connector
        for evse in self.evses.values():
            await self._send_status(evse.evse_id, evse.connector_id, evse.status)

        # Heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # SecurityEventNotification on boot
        await self._call(
            call201.SecurityEventNotificationPayload(
                type="StartupOfTheDevice",
                timestamp=_now_iso(),
            )
        )

    async def _cleanup(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for evse in self.evses.values():
            if evse.meter_task:
                evse.meter_task.cancel()
                evse.meter_task = None
        if self._firmware_task:
            self._firmware_task.cancel()
            self._firmware_task = None
        self._connected = False

    # ─── OCPP calls ──────────────────────────────────────────────────────

    async def _call(self, payload):
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

    async def _send_status(self, evse_id: int, connector_id: int, status: str) -> None:
        if evse_id in self.evses:
            self.evses[evse_id].status = status
        await self._call(
            call201.StatusNotificationPayload(
                timestamp=_now_iso(),
                connector_status=status,
                evse_id=evse_id,
                connector_id=connector_id,
            )
        )

    # ─── Heartbeat ───────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        try:
            while self._connected:
                interval = int(self._variables.get("OCPPCommCtrlr.HeartbeatInterval", {}).get("value", "30"))
                await asyncio.sleep(interval)
                if not self._connected:
                    break
                await self._call(call201.HeartbeatPayload())
        except asyncio.CancelledError:
            pass

    # ─── TransactionEvent ────────────────────────────────────────────────

    async def _send_transaction_event(
        self,
        event_type: str,
        evse: EVSEState,
        trigger: str = "Authorized",
        meter_values: list = None,
    ) -> Optional[object]:
        evse.seq_no += 1
        payload_dict = {
            "event_type": event_type,
            "timestamp": _now_iso(),
            "trigger_reason": trigger,
            "seq_no": evse.seq_no,
            "transaction_info": {
                "transaction_id": evse.transaction_id,
                "charging_state": evse.charging_state,
            },
        }

        if evse.id_token:
            payload_dict["id_token"] = evse.id_token

        if evse.evse_id:
            payload_dict["evse"] = {"id": evse.evse_id, "connector_id": evse.connector_id}

        if meter_values:
            payload_dict["meter_value"] = meter_values

        return await self._call(
            call201.TransactionEventPayload(**payload_dict)
        )

    # ─── Charging sessions ───────────────────────────────────────────────

    async def start_charging(self, evse_id: int = 1, id_token_value: str = "VIRTUAL-TAG") -> bool:
        evse = self.evses.get(evse_id)
        if not evse or evse.transaction_id is not None or not evse.available:
            return False
        asyncio.create_task(self._do_start_charging(evse_id, id_token_value))
        return True

    async def _do_start_charging(self, evse_id: int, id_token_value: str) -> None:
        evse = self.evses[evse_id]

        # Preparing
        await self._send_status(evse_id, evse.connector_id, ConnectorStatusType.occupied)

        txn_id = self._next_txn_id()
        evse.transaction_id = txn_id
        evse.id_token = {"id_token": id_token_value, "type": IdTokenType.iso14443}
        evse.charging_state = ChargingStateType.ev_connected
        evse.seq_no = 0

        # TransactionEvent Started
        await self._send_transaction_event(
            TransactionEventType.started, evse, trigger="Authorized"
        )

        await asyncio.sleep(random.uniform(1, 3))

        # Init physics
        soc = random_start_soc()
        meter_start = self._cumulative_wh.get(evse_id, 0)
        env_conds = EnvironmentConditions(ambient_temp_c=self.env_sim.site.ambient_temp_c)
        evse.charge_state = ChargeState(
            soc=soc,
            meter_wh=float(meter_start),
            max_kw=self.profile.max_kw,
        )
        evse.charge_state.env = env_conds
        evse.charge_state.direction = PowerDirection.CHARGING
        evse.charging_state = ChargingStateType.charging

        # TransactionEvent Updated (charging started)
        await self._send_transaction_event(
            TransactionEventType.updated, evse, trigger="ChargingStateChanged"
        )

        farm_metrics.record_session_started()
        farm_metrics.log_event("info", self.cp_id, f"Started txn={txn_id} evse={evse_id} soc={soc:.1f}%")

        # Start meter loop
        evse.meter_task = asyncio.create_task(self._meter_loop(evse_id))

    async def stop_charging(self, evse_id: int = 1, reason: str = "Remote") -> bool:
        evse = self.evses.get(evse_id)
        if not evse or evse.transaction_id is None:
            return False
        asyncio.create_task(self._do_stop_charging(evse_id, reason))
        return True

    async def _do_stop_charging(self, evse_id: int, reason: str = "Remote") -> None:
        evse = self.evses[evse_id]
        if evse.transaction_id is None:
            return

        if evse.meter_task:
            evse.meter_task.cancel()
            try:
                await evse.meter_task
            except asyncio.CancelledError:
                pass
            evse.meter_task = None

        meter_stop = int(evse.charge_state.meter_wh) if evse.charge_state else int(self._cumulative_wh.get(evse_id, 0))
        self._cumulative_wh[evse_id] = float(meter_stop)
        if evse.charge_state:
            self._cumulative_export_wh[evse_id] = evse.charge_state.discharge_meter_wh

        evse.charging_state = "Idle"

        # TransactionEvent Ended
        meter_values = self._build_meter_values(evse) if evse.charge_state else None
        await self._send_transaction_event(
            TransactionEventType.ended, evse,
            trigger="StopAuthorized" if reason == "Remote" else "EVDeparted",
            meter_values=meter_values,
        )

        farm_metrics.record_session_ended()
        farm_metrics.log_event("info", self.cp_id, f"Stopped txn={evse.transaction_id} evse={evse_id}")

        evse.transaction_id = None
        evse.id_token = None
        evse.charge_state = None

        await self._send_status(evse_id, evse.connector_id, ConnectorStatusType.available)

    # ─── V2G ─────────────────────────────────────────────────────────────

    async def start_v2g(self, evse_id: int = 1, max_discharge_kw: float = 50.0, min_soc: float = 20.0) -> bool:
        evse = self.evses.get(evse_id)
        if not evse or not evse.charge_state or evse.transaction_id is None:
            return False
        evse.charge_state.v2g = V2GConfig(enabled=True, max_discharge_kw=max_discharge_kw, min_soc_floor=min_soc)
        evse.charge_state.direction = PowerDirection.DISCHARGING
        evse.charging_state = "Discharging"

        await self._send_transaction_event(
            TransactionEventType.updated, evse, trigger="ChargingStateChanged"
        )
        farm_metrics.log_event("info", self.cp_id, f"V2G started evse={evse_id}")
        return True

    async def stop_v2g(self, evse_id: int = 1) -> bool:
        evse = self.evses.get(evse_id)
        if not evse or not evse.charge_state:
            return False
        evse.charge_state.v2g.enabled = False
        evse.charge_state.direction = PowerDirection.CHARGING
        evse.charging_state = ChargingStateType.charging
        farm_metrics.log_event("info", self.cp_id, f"V2G stopped evse={evse_id}")
        return True

    # ─── Meter values ────────────────────────────────────────────────────

    def _build_meter_values(self, evse: EVSEState) -> list:
        if not evse.charge_state:
            return []
        cs = evse.charge_state
        snap = cs.tick(0)  # 0-second tick for current values

        sampled = [
            {"sampled_value": [
                {"value": snap.energy_wh, "measurand": "Energy.Active.Import.Register", "unit_of_measure": {"unit": "Wh"}},
                {"value": snap.power_w, "measurand": "Power.Active.Import", "unit_of_measure": {"unit": "W"}},
                {"value": snap.current_a, "measurand": "Current.Import", "unit_of_measure": {"unit": "A"}},
                {"value": snap.voltage, "measurand": "Voltage", "unit_of_measure": {"unit": "V"}},
                {"value": snap.soc, "measurand": "SoC", "unit_of_measure": {"unit": "Percent"}},
            ], "timestamp": _now_iso()}
        ]

        if snap.power_export_w > 0:
            sampled[0]["sampled_value"].extend([
                {"value": snap.energy_export_wh, "measurand": "Energy.Active.Export.Register", "unit_of_measure": {"unit": "Wh"}},
                {"value": snap.power_export_w, "measurand": "Power.Active.Export", "unit_of_measure": {"unit": "W"}},
            ])

        return sampled

    async def _meter_loop(self, evse_id: int) -> None:
        evse = self.evses[evse_id]
        try:
            while evse.transaction_id is not None and evse.charge_state is not None:
                interval = int(self._variables.get("SampledDataCtrlr.TxUpdatedInterval", {}).get("value", "30"))
                await asyncio.sleep(interval)

                if evse.transaction_id is None or evse.charge_state is None:
                    break

                # Check faults
                fault = self.env_sim.has_session_stopping_fault(self.cp_id, evse_id)
                if fault:
                    farm_metrics.log_event("warning", self.cp_id, f"Fault stops session: {fault.fault_type.value}")
                    await self._do_stop_charging(evse_id, "Other")
                    return

                # Apply derating
                evse.charge_state.hardware_derating = self.env_sim.get_derating_factor(self.cp_id)
                evse.charge_state.env.ambient_temp_c = self.env_sim.site.ambient_temp_c

                # Physics tick
                snap = evse.charge_state.tick(float(interval))
                self._cumulative_wh[evse_id] = evse.charge_state.meter_wh

                if self._connected:
                    meter_values = self._build_meter_values(evse)
                    await self._send_transaction_event(
                        TransactionEventType.updated, evse,
                        trigger="MeterValuePeriodic",
                        meter_values=meter_values,
                    )
                else:
                    self.network.offline_buffer.queue_meter_value({
                        "type": "TransactionEvent",
                        "evse_id": evse_id,
                        "transaction_id": evse.transaction_id,
                        "timestamp": _now_iso(),
                        "snapshot": snap.as_dict(),
                    })

                if evse.charge_state.is_full:
                    farm_metrics.log_event("info", self.cp_id, f"SoC 100% evse={evse_id}")
                    await self._do_stop_charging(evse_id, "Local")
                    return

        except asyncio.CancelledError:
            pass

    # ─── Offline replay ──────────────────────────────────────────────────

    async def _replay_offline_buffer(self) -> None:
        queued = self.network.offline_buffer.drain_meter_values()
        if not queued:
            return
        farm_metrics.log_event("info", self.cp_id, f"Replaying {len(queued)} offline messages")
        for msg in queued:
            await asyncio.sleep(0.1)
            # Simplified replay — just log that we replayed
            farm_metrics.log_event("info", self.cp_id, f"Replayed offline: {msg['type']}")

    # ─── PnC flow ────────────────────────────────────────────────────────

    async def trigger_pnc(self, evse_id: int = 1) -> bool:
        if not self.pnc_config.enabled:
            return False

        self.pnc_config.contract_id_counter += 1
        emaid = generate_emaid(self.pnc_config.emaid_prefix, self.pnc_config.contract_id_counter)
        exi = generate_exi_cert_request(emaid)

        await asyncio.sleep(self.pnc_config.tls_handshake_delay_sec)

        # Get15118EVCertificate
        await self._call(
            call201.Get15118EVCertificatePayload(
                iso15118_schema_version="urn:iso:15118:2:2013:MsgDef" if not self.pnc_config.iso15118_20 else "urn:iso:std:iso:15118:-20:DC",
                action="Install",
                exi_request=exi,
            )
        )

        # Start charging with eMAID
        await self.start_charging(evse_id, emaid)
        farm_metrics.log_event("info", self.cp_id, f"PnC 2.0.1 flow started emaid={emaid}")
        return True

    # ─── Error injection ─────────────────────────────────────────────────

    async def inject_error(self, error_code: str, evse_id: int = 1) -> None:
        await self._send_status(evse_id, 1, ConnectorStatusType.faulted)
        farm_metrics.log_event("warning", self.cp_id, f"Error injected: {error_code} evse={evse_id}")

    # ─── Force disconnect / reconnect ────────────────────────────────────

    async def force_disconnect(self) -> None:
        self._connected = False
        self.network.go_offline()
        if self._ws:
            await self._ws.close()
        farm_metrics.log_event("warning", self.cp_id, "Forced disconnect")

    async def force_reconnect(self) -> None:
        await self.force_disconnect()

    # ─── Firmware update ─────────────────────────────────────────────────

    async def _simulate_firmware_update(self, location: str) -> None:
        try:
            for status in [FirmwareStatusType.downloading, FirmwareStatusType.downloaded,
                           FirmwareStatusType.installing]:
                self._firmware_status = status
                await self._call(call201.FirmwareStatusNotificationPayload(status=status))
                await asyncio.sleep(random.uniform(3, 10))

            if random.random() < 0.9:
                self._firmware_status = FirmwareStatusType.installed
                await self._call(call201.FirmwareStatusNotificationPayload(status=FirmwareStatusType.installed))
            else:
                self._firmware_status = FirmwareStatusType.installation_failed
                await self._call(call201.FirmwareStatusNotificationPayload(status=FirmwareStatusType.installation_failed))
        except asyncio.CancelledError:
            pass


# ─── OCPP 2.0.1 Message Handler ─────────────────────────────────────────────


class _ChargePointHandler201(CP201):
    def __init__(self, cp_id: str, ws, charger: VirtualCharger201):
        super().__init__(cp_id, ws)
        self.charger = charger

    @on(Action.RequestStartTransaction)
    async def on_request_start(self, id_token: dict, evse_id: int = 1, **kwargs):
        farm_metrics.record_message_received()
        evse = self.charger.evses.get(evse_id)
        if not evse or evse.transaction_id is not None or not evse.available:
            return call_result201.RequestStartTransactionPayload(status=RequestStartStopStatusType.rejected)

        token_value = id_token.get("id_token", "REMOTE")
        asyncio.create_task(self.charger._do_start_charging(evse_id, token_value))
        return call_result201.RequestStartTransactionPayload(status=RequestStartStopStatusType.accepted)

    @on(Action.RequestStopTransaction)
    async def on_request_stop(self, transaction_id: str, **kwargs):
        farm_metrics.record_message_received()
        for evse in self.charger.evses.values():
            if evse.transaction_id == transaction_id:
                asyncio.create_task(self.charger._do_stop_charging(evse.evse_id, "Remote"))
                return call_result201.RequestStopTransactionPayload(status=RequestStartStopStatusType.accepted)
        return call_result201.RequestStopTransactionPayload(status=RequestStartStopStatusType.rejected)

    @on(Action.GetVariables)
    async def on_get_variables(self, get_variable_data: list, **kwargs):
        farm_metrics.record_message_received()
        results = []
        for req in get_variable_data:
            comp = req.get("component", {}).get("name", "")
            var = req.get("variable", {}).get("name", "")
            key = f"{comp}.{var}"
            if key in self.charger._variables:
                results.append({
                    "attribute_status": GetVariableStatusType.accepted,
                    "component": req["component"],
                    "variable": req["variable"],
                    "attribute_value": self.charger._variables[key]["value"],
                })
            else:
                results.append({
                    "attribute_status": GetVariableStatusType.unknown_variable,
                    "component": req["component"],
                    "variable": req["variable"],
                })
        return call_result201.GetVariablesPayload(get_variable_result=results)

    @on(Action.SetVariables)
    async def on_set_variables(self, set_variable_data: list, **kwargs):
        farm_metrics.record_message_received()
        results = []
        for req in set_variable_data:
            comp = req.get("component", {}).get("name", "")
            var = req.get("variable", {}).get("name", "")
            key = f"{comp}.{var}"
            if key in self.charger._variables:
                if self.charger._variables[key]["mutability"] == "ReadOnly":
                    results.append({
                        "attribute_status": SetVariableStatusType.rejected,
                        "component": req["component"],
                        "variable": req["variable"],
                    })
                else:
                    self.charger._variables[key]["value"] = req.get("attribute_value", "")
                    results.append({
                        "attribute_status": SetVariableStatusType.accepted,
                        "component": req["component"],
                        "variable": req["variable"],
                    })
            else:
                results.append({
                    "attribute_status": SetVariableStatusType.unknown_variable,
                    "component": req["component"],
                    "variable": req["variable"],
                })
        return call_result201.SetVariablesPayload(set_variable_result=results)

    @on(Action.SetChargingProfile)
    async def on_set_charging_profile(self, evse_id: int, charging_profile: dict, **kwargs):
        farm_metrics.record_message_received()
        sl = charging_profile.get("stack_level", 0)
        charging_profile["evse_id"] = evse_id
        self.charger._charging_profiles[sl] = charging_profile
        return call_result201.SetChargingProfilePayload(status="Accepted")

    @on(Action.ClearChargingProfile)
    async def on_clear_charging_profile(self, **kwargs):
        farm_metrics.record_message_received()
        self.charger._charging_profiles.clear()
        return call_result201.ClearChargingProfilePayload(status=ClearChargingProfileStatusType.accepted)

    @on(Action.GetCompositeSchedule)
    async def on_get_composite_schedule(self, duration: int, evse_id: int, **kwargs):
        farm_metrics.record_message_received()
        return call_result201.GetCompositeSchedulePayload(status="Accepted")

    @on(Action.Reset)
    async def on_reset(self, type: str, **kwargs):
        farm_metrics.record_message_received()
        for evse in self.charger.evses.values():
            if evse.transaction_id is not None:
                await self.charger._do_stop_charging(evse.evse_id, "Reboot")
        if type == ResetType.immediate:
            asyncio.create_task(self.charger.force_disconnect())
        return call_result201.ResetPayload(status=ResetStatusType.accepted)

    @on(Action.TriggerMessage)
    async def on_trigger_message(self, requested_message: str, **kwargs):
        farm_metrics.record_message_received()
        if requested_message == TriggerMessageType.heartbeat:
            asyncio.create_task(self.charger._call(call201.HeartbeatPayload()))
            return call_result201.TriggerMessagePayload(status=TriggerMessageStatusType.accepted)
        elif requested_message == TriggerMessageType.boot_notification:
            asyncio.create_task(self.charger._call(
                call201.BootNotificationPayload(
                    charging_station={"model": self.charger.profile.model, "vendor_name": self.charger.profile.vendor},
                    reason=BootReasonType.triggered,
                )
            ))
            return call_result201.TriggerMessagePayload(status=TriggerMessageStatusType.accepted)
        elif requested_message == TriggerMessageType.status_notification:
            evse_id = kwargs.get("evse", {}).get("id", 1) if "evse" in kwargs else 1
            evse = self.charger.evses.get(evse_id)
            if evse:
                asyncio.create_task(self.charger._send_status(evse_id, evse.connector_id, evse.status))
                return call_result201.TriggerMessagePayload(status=TriggerMessageStatusType.accepted)
        elif requested_message == TriggerMessageType.firmware_status_notification:
            if self.charger._firmware_status:
                asyncio.create_task(self.charger._call(
                    call201.FirmwareStatusNotificationPayload(status=self.charger._firmware_status)
                ))
                return call_result201.TriggerMessagePayload(status=TriggerMessageStatusType.accepted)
        return call_result201.TriggerMessagePayload(status=TriggerMessageStatusType.not_implemented)

    @on(Action.ChangeAvailability)
    async def on_change_availability(self, operational_status: str, **kwargs):
        farm_metrics.record_message_received()
        evse_data = kwargs.get("evse")
        if evse_data:
            evse_id = evse_data.get("id", 1)
            evse = self.charger.evses.get(evse_id)
            if evse:
                evse.available = (operational_status == OperationalStatusType.operative)
                if evse.transaction_id is None:
                    status = ConnectorStatusType.available if evse.available else ConnectorStatusType.unavailable
                    asyncio.create_task(self.charger._send_status(evse_id, evse.connector_id, status))
                return call_result201.ChangeAvailabilityPayload(status="Accepted")
        else:
            for evse in self.charger.evses.values():
                evse.available = (operational_status == OperationalStatusType.operative)
            return call_result201.ChangeAvailabilityPayload(status="Accepted")
        return call_result201.ChangeAvailabilityPayload(status="Rejected")

    @on(Action.ReserveNow)
    async def on_reserve_now(self, id: int, expiry_date_time: str, id_token: dict, **kwargs):
        farm_metrics.record_message_received()
        evse_data = kwargs.get("evse")
        evse_id = evse_data.get("id", 1) if evse_data else 1
        evse = self.charger.evses.get(evse_id)
        if not evse or evse.transaction_id is not None:
            return call_result201.ReserveNowPayload(status=ReservationUpdateStatusType.rejected)
        evse.reservation_id = id
        return call_result201.ReserveNowPayload(status=ReservationUpdateStatusType.accepted)

    @on(Action.CancelReservation)
    async def on_cancel_reservation(self, reservation_id: int, **kwargs):
        farm_metrics.record_message_received()
        for evse in self.charger.evses.values():
            if evse.reservation_id == reservation_id:
                evse.reservation_id = None
                return call_result201.CancelReservationPayload(status="Accepted")
        return call_result201.CancelReservationPayload(status="Rejected")

    @on(Action.UpdateFirmware)
    async def on_update_firmware(self, request_id: int, firmware: dict, **kwargs):
        farm_metrics.record_message_received()
        location = firmware.get("location", "")
        if self.charger._firmware_task:
            self.charger._firmware_task.cancel()
        self.charger._firmware_task = asyncio.create_task(
            self.charger._simulate_firmware_update(location)
        )
        return call_result201.UpdateFirmwarePayload(status=UpdateFirmwareStatusType.accepted)

    @on(Action.InstallCertificate)
    async def on_install_certificate(self, certificate_type: str, certificate: str, **kwargs):
        farm_metrics.record_message_received()
        farm_metrics.log_event("info", self.charger.cp_id, f"InstallCertificate type={certificate_type}")
        return call_result201.InstallCertificatePayload(status="Accepted")

    @on(Action.CertificateSigned)
    async def on_certificate_signed(self, certificate_chain: str, **kwargs):
        farm_metrics.record_message_received()
        farm_metrics.log_event("info", self.charger.cp_id, "CertificateSigned received")
        return call_result201.CertificateSignedPayload(status="Accepted")

    @on(Action.DataTransfer)
    async def on_data_transfer(self, vendor_id: str, **kwargs):
        farm_metrics.record_message_received()
        return call_result201.DataTransferPayload(status=DataTransferStatusType.accepted)

    @on(Action.GetBaseReport)
    async def on_get_base_report(self, request_id: int, report_base: str, **kwargs):
        farm_metrics.record_message_received()
        # Send NotifyReport with device model data
        asyncio.create_task(self._send_device_report(request_id))
        return call_result201.GetBaseReportPayload(status="Accepted")

    async def _send_device_report(self, request_id: int):
        report_data = []
        for key, val in self.charger._variables.items():
            parts = key.split(".", 1)
            report_data.append({
                "component": {"name": parts[0]},
                "variable": {"name": parts[1] if len(parts) > 1 else parts[0]},
                "variable_attribute": [{"value": val["value"], "mutability": val["mutability"]}],
            })
        try:
            await self.charger._call(
                call201.NotifyReportPayload(
                    request_id=request_id,
                    generated_at=_now_iso(),
                    seq_no=0,
                    report_data=report_data,
                )
            )
        except Exception:
            pass
