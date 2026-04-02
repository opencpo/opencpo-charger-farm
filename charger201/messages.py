"""
OCPP 2.0.1 Virtual Charger — status notification and meter value helpers.
"""

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ocpp.v201 import call as call201
from ocpp.v201.enums import ConnectorStatusType

from metrics import farm_metrics

if TYPE_CHECKING:
    from .core import VirtualCharger201, EVSEState

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def send_status(charger: "VirtualCharger201", evse_id: int, connector_id: int, status: str) -> None:
    if evse_id in charger.evses:
        charger.evses[evse_id].status = status
    await charger._call(
        call201.StatusNotificationPayload(
            timestamp=_now_iso(),
            connector_status=status,
            evse_id=evse_id,
            connector_id=connector_id,
        )
    )


def build_meter_values(evse: "EVSEState") -> list:
    if not evse.charge_state:
        return []
    cs = evse.charge_state
    snap = cs.tick(0)

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


async def inject_error(charger: "VirtualCharger201", error_code: str, evse_id: int = 1) -> None:
    await send_status(charger, evse_id, 1, ConnectorStatusType.faulted)
    farm_metrics.log_event("warning", charger.cp_id, f"Error injected: {error_code} evse={evse_id}")
