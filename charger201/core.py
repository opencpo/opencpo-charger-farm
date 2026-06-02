"""
OCPP 2.0.1 Virtual Charger — VirtualCharger201 class (state & public API).
"""

import asyncio
import logging
import random
import time
from typing import Optional

from ocpp.v201.enums import ConnectorStatusEnumType

from profiles import ChargerProfile, QuirkConfig
from network import NetworkLayer, ConnectionState
from environment import EnvironmentSimulator
from pnc import PnCConfig
from metrics import farm_metrics

log = logging.getLogger(__name__)


class EVSEState:
    """State for a single EVSE (2.0.1 model: evse has connectors)."""

    def __init__(self, evse_id: int, connector_id: int = 1):
        self.evse_id = evse_id
        self.connector_id = connector_id
        self.status: str = ConnectorStatusEnumType.available
        self.transaction_id: Optional[str] = None
        self.id_token: Optional[dict] = None
        self.charge_state = None
        self.meter_task: Optional[asyncio.Task] = None
        self.charging_state: str = "Idle"
        self.seq_no: int = 0
        self.reservation_id: Optional[int] = None
        self.available: bool = True


class VirtualCharger201:
    """
    OCPP 2.0.1 virtual charger instance.
    Coordinates connection, session, and message modules.
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

        self.network = NetworkLayer(cp_id)

        self.evses: dict[int, EVSEState] = {}
        for i in range(1, profile.num_connectors + 1):
            self.evses[i] = EVSEState(evse_id=i, connector_id=1)

        self._variables: dict[str, dict] = {
            "OCPPCommCtrlr.HeartbeatInterval": {"value": "30", "mutability": "ReadWrite"},
            "SampledDataCtrlr.TxUpdatedInterval": {"value": "30", "mutability": "ReadWrite"},
            "ChargingStation.Model": {"value": profile.model, "mutability": "ReadOnly"},
            "ChargingStation.VendorName": {"value": profile.vendor, "mutability": "ReadOnly"},
            "ChargingStation.FirmwareVersion": {"value": profile.firmware, "mutability": "ReadOnly"},
            "ChargingStation.SerialNumber": {"value": f"{cp_id}-SN", "mutability": "ReadOnly"},
            "SecurityCtrlr.SecurityProfile": {"value": "1", "mutability": "ReadWrite"},
        }

        self._charging_profiles: dict[int, dict] = {}
        self._firmware_status: Optional[str] = None
        self._firmware_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cp = None
        self._ws = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._connected = False

        self._cumulative_wh: dict[int, float] = {i: random.uniform(0, 50000) for i in range(1, profile.num_connectors + 1)}
        self._cumulative_export_wh: dict[int, float] = {i: 0.0 for i in range(1, profile.num_connectors + 1)}
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

    # ─── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        from .connection import run_connection_loop
        await run_connection_loop(self)

    async def stop(self) -> None:
        from .connection import cleanup
        self._running = False
        self._shutdown_event.set()
        await cleanup(self)

    async def start_charging(self, evse_id: int = 1, id_token_value: str = "VIRTUAL-TAG") -> bool:
        from .session import start_charging
        return await start_charging(self, evse_id, id_token_value)

    async def stop_charging(self, evse_id: int = 1, reason: str = "Remote") -> bool:
        from .session import stop_charging
        return await stop_charging(self, evse_id, reason)

    async def start_v2g(self, evse_id: int = 1, max_discharge_kw: float = 50.0,
                         min_soc: float = 20.0) -> bool:
        from .session import start_v2g
        return await start_v2g(self, evse_id, max_discharge_kw, min_soc)

    async def stop_v2g(self, evse_id: int = 1) -> bool:
        from .session import stop_v2g
        return await stop_v2g(self, evse_id)

    async def trigger_pnc(self, evse_id: int = 1) -> bool:
        from .session import trigger_pnc
        return await trigger_pnc(self, evse_id)

    async def inject_error(self, error_code: str, evse_id: int = 1) -> None:
        from .messages import inject_error
        await inject_error(self, error_code, evse_id)

    async def force_disconnect(self) -> None:
        self._connected = False
        self.network.go_offline()
        if self._ws:
            await self._ws.close()
        farm_metrics.log_event("warning", self.cp_id, "Forced disconnect")

    async def force_reconnect(self) -> None:
        await self.force_disconnect()

    # ─── Internal helpers (used by submodules) ───────────────────────────

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
        from .messages import send_status
        await send_status(self, evse_id, connector_id, status)
