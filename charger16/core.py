"""
OCPP 1.6j Virtual Charger — VirtualCharger16 class (state & public API).
"""

import asyncio
import logging
import random
import time
from typing import Optional

from ocpp.v16.enums import ChargePointStatus

from profiles import ChargerProfile, QuirkConfig
from network import NetworkLayer, ConnectionState
from environment import EnvironmentSimulator
from pnc import PnCConfig
from metrics import farm_metrics

log = logging.getLogger(__name__)


class ConnectorState:
    """State for a single connector."""

    def __init__(self, connector_id: int):
        self.connector_id = connector_id
        self.status: str = ChargePointStatus.available
        self.transaction_id: Optional[int] = None
        self.id_tag: Optional[str] = None
        self.charge_state = None
        self.meter_task: Optional[asyncio.Task] = None
        self.reservation_id: Optional[int] = None
        self.reserved_id_tag: Optional[str] = None
        self.available: bool = True


class VirtualCharger16:
    """
    OCPP 1.6j virtual charger instance.
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

        self.connectors: dict[int, ConnectorState] = {}
        for i in range(1, profile.num_connectors + 1):
            self.connectors[i] = ConnectorState(i)

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

        self._local_auth_list: dict[str, str] = {}
        self._local_auth_version: int = 0
        self._charging_profiles: dict[int, dict] = {}
        self._firmware_status: Optional[str] = None
        self._firmware_task: Optional[asyncio.Task] = None
        self._diagnostics_status: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cp = None
        self._ws = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._connected = False

        self._cumulative_wh: dict[int, float] = {i: random.uniform(0, 50000) for i in range(1, profile.num_connectors + 1)}
        self._cumulative_export_wh: dict[int, float] = {i: 0.0 for i in range(1, profile.num_connectors + 1)}
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

    # ─── Public API ──────────────────────────────────────────────────────

    async def start(self) -> None:
        from .connection import run_connection_loop
        await run_connection_loop(self)

    async def stop(self) -> None:
        from .connection import cleanup
        self._running = False
        self._shutdown_event.set()
        await cleanup(self)

    async def start_charging(self, connector_id: int = 1, id_tag: str = "VIRTUAL-TAG") -> bool:
        from .session import start_charging
        return await start_charging(self, connector_id, id_tag)

    async def stop_charging(self, connector_id: int = 1, reason: str = "Remote") -> bool:
        from .session import stop_charging
        return await stop_charging(self, connector_id, reason)

    async def start_v2g(self, connector_id: int = 1, max_discharge_kw: float = 50.0,
                         min_soc: float = 20.0) -> bool:
        from .session import start_v2g
        return await start_v2g(self, connector_id, max_discharge_kw, min_soc)

    async def stop_v2g(self, connector_id: int = 1) -> bool:
        from .session import stop_v2g
        return await stop_v2g(self, connector_id)

    async def trigger_pnc(self, connector_id: int = 1) -> bool:
        from .session import trigger_pnc
        return await trigger_pnc(self, connector_id)

    async def inject_error(self, error_code: str, connector_id: int = 1) -> None:
        from .messages import inject_error
        await inject_error(self, error_code, connector_id)

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

    async def _send_status(self, connector_id: int, status: str) -> None:
        from .messages import send_status
        await send_status(self, connector_id, status)

    async def _send_status_with_error(self, connector_id: int, status: str,
                                       error_code: str, info: str = "") -> None:
        from .messages import send_status_with_error
        await send_status_with_error(self, connector_id, status, error_code, info)
