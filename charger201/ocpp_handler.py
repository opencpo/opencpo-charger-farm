"""
OCPP 2.0.1 Virtual Charger — ChargePointHandler class (incoming command handler).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ocpp.v201 import ChargePoint as CP201
from ocpp.v201 import call as call201
from ocpp.v201 import call_result as call_result201
from ocpp.v201.enums import (
    Action,
    BootReasonType,
    ClearChargingProfileStatusType,
    ConnectorStatusEnumType,
    DataTransferStatusType,
    GetVariableStatusType,
    OperationalStatusType,
    RequestStartStopStatusType,
    ReservationUpdateStatusType,
    ResetStatusType,
    ResetType,
    SetVariableStatusType,
    TriggerMessageStatusType,
    TriggerMessageType,
    UpdateFirmwareStatusType,
)
from ocpp.routing import on

from metrics import farm_metrics

if TYPE_CHECKING:
    from .core import VirtualCharger201

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ChargePointHandler201(CP201):
    def __init__(self, cp_id: str, ws, charger: "VirtualCharger201"):
        super().__init__(cp_id, ws)
        self.charger = charger

    @on(Action.RequestStartTransaction)
    async def on_request_start(self, id_token: dict, evse_id: int = 1, **kwargs):
        from .session import do_start_charging
        farm_metrics.record_message_received()
        evse = self.charger.evses.get(evse_id)
        if not evse or evse.transaction_id is not None or not evse.available:
            return call_result201.RequestStartTransactionPayload(status=RequestStartStopStatusType.rejected)
        token_value = id_token.get("id_token", "REMOTE")
        asyncio.create_task(do_start_charging(self.charger, evse_id, token_value))
        return call_result201.RequestStartTransactionPayload(status=RequestStartStopStatusType.accepted)

    @on(Action.RequestStopTransaction)
    async def on_request_stop(self, transaction_id: str, **kwargs):
        from .session import do_stop_charging
        farm_metrics.record_message_received()
        for evse in self.charger.evses.values():
            if evse.transaction_id == transaction_id:
                asyncio.create_task(do_stop_charging(self.charger, evse.evse_id, "Remote"))
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
                    results.append({"attribute_status": SetVariableStatusType.rejected,
                                    "component": req["component"], "variable": req["variable"]})
                else:
                    self.charger._variables[key]["value"] = req.get("attribute_value", "")
                    results.append({"attribute_status": SetVariableStatusType.accepted,
                                    "component": req["component"], "variable": req["variable"]})
            else:
                results.append({"attribute_status": SetVariableStatusType.unknown_variable,
                                "component": req["component"], "variable": req["variable"]})
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
        from .session import do_stop_charging
        farm_metrics.record_message_received()
        for evse in self.charger.evses.values():
            if evse.transaction_id is not None:
                await do_stop_charging(self.charger, evse.evse_id, "Reboot")
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
                    status = ConnectorStatusEnumType.available if evse.available else ConnectorStatusEnumType.unavailable
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
        from .connection import simulate_firmware_update
        farm_metrics.record_message_received()
        location = firmware.get("location", "")
        if self.charger._firmware_task:
            self.charger._firmware_task.cancel()
        self.charger._firmware_task = asyncio.create_task(
            simulate_firmware_update(self.charger, location)
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
