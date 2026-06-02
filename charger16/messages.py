"""
OCPP 1.6j Virtual Charger — meter values and status notification helpers.
"""

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ocpp.v16 import call
from ocpp.v16.enums import (
    ChargePointErrorCode,
    ChargePointStatus,
    Measurand,
    UnitOfMeasure,
)

from metrics import farm_metrics
from physics import MeterSnapshot, PowerDirection

if TYPE_CHECKING:
    from .core import VirtualCharger16

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def send_status(charger: "VirtualCharger16", connector_id: int, status: str) -> None:
    """Send StatusNotification and update internal state."""
    if connector_id > 0 and connector_id in charger.connectors:
        charger.connectors[connector_id].status = status
    await charger._call(
        call.StatusNotification(
            connector_id=connector_id,
            error_code=ChargePointErrorCode.no_error,
            status=status,
        )
    )


async def send_status_with_error(charger: "VirtualCharger16", connector_id: int,
                                  status: str, error_code: str, info: str = "") -> None:
    """Send StatusNotification with error code."""
    if connector_id > 0 and connector_id in charger.connectors:
        charger.connectors[connector_id].status = status
    await charger._call(
        call.StatusNotification(
            connector_id=connector_id,
            error_code=error_code,
            status=status,
            info=info,
        )
    )


async def send_meter_values(charger: "VirtualCharger16", connector_id: int,
                             transaction_id: int, snap: MeterSnapshot) -> None:
    ts = _now_iso()

    def _val(v, as_str: bool) -> str:
        return str(v)

    as_str = charger.quirks.meter_values_as_strings

    sampled = [
        {"value": _val(snap.energy_wh, as_str), "measurand": Measurand.energy_active_import_register, "unit": UnitOfMeasure.wh},
    ]
    # Maxpower quirk: does NOT send Power.Active.Import
    if not charger.quirks.no_power_measurand:
        sampled.append({"value": _val(snap.power_w, as_str), "measurand": Measurand.power_active_import, "unit": UnitOfMeasure.w})
    sampled.extend([
        {"value": _val(snap.current_a, as_str), "measurand": Measurand.current_import, "unit": UnitOfMeasure.a},
        {"value": _val(snap.soc, as_str), "measurand": Measurand.soc, "unit": UnitOfMeasure.percent},
        {"value": _val(snap.voltage, as_str), "measurand": Measurand.voltage, "unit": UnitOfMeasure.v},
    ])

    if snap.power_export_w > 0:
        sampled.extend([
            {"value": _val(snap.energy_export_wh, as_str), "measurand": Measurand.energy_active_export_register, "unit": UnitOfMeasure.wh},
            {"value": _val(snap.power_export_w, as_str), "measurand": Measurand.power_active_export, "unit": UnitOfMeasure.w},
        ])

    await charger._call(
        call.MeterValues(
            connector_id=connector_id,
            transaction_id=transaction_id,
            meter_value=[{"timestamp": ts, "sampled_value": sampled}],
        )
    )


async def inject_error(charger: "VirtualCharger16", error_code: str, connector_id: int = 1) -> None:
    """Inject an OCPP error on a connector."""
    await send_status_with_error(
        charger, connector_id, ChargePointStatus.faulted, error_code, f"Injected error: {error_code}"
    )
    farm_metrics.log_event("warning", charger.cp_id, f"Error injected: {error_code} conn={connector_id}")
