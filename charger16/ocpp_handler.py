"""
OCPP 1.6j Virtual Charger — ChargePointHandler class (incoming command handler).
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ocpp.v16 import ChargePoint as CP16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointStatus,
    ClearChargingProfileStatus,
    ConfigurationStatus,
    DataTransferStatus,
    DiagnosticsStatus,
    FirmwareStatus,
    RemoteStartStopStatus,
    ReservationStatus,
    ResetStatus,
    ResetType,
    TriggerMessageStatus,
    UpdateStatus,
)
from ocpp.routing import on

from metrics import farm_metrics

if TYPE_CHECKING:
    from .core import VirtualCharger16

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ChargePointHandler(CP16):
    """Internal OCPP 1.6j handler that delegates to VirtualCharger16."""

    def __init__(self, cp_id: str, ws, charger: "VirtualCharger16"):
        super().__init__(cp_id, ws)
        self.charger = charger

    @on(Action.remote_start_transaction)
    async def on_remote_start(self, id_tag: str, connector_id: int = 1, **kwargs):
        from .session import do_authorize_then_start, do_start_charging
        farm_metrics.record_message_received()
        conn = self.charger.connectors.get(connector_id)
        if not conn or conn.transaction_id is not None:
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.rejected)

        if self.charger.quirks.remote_start_needs_cable and conn.available:
            log.info(f"[{self.charger.cp_id}] RemoteStart accepted but no car on connector {connector_id} — silent ignore")
            return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

        if "charging_profile" in kwargs and kwargs["charging_profile"]:
            prof = kwargs["charging_profile"]
            sl = prof.get("stack_level", 0)
            self.charger._charging_profiles[sl] = prof

        if self.charger.quirks.authorize_before_start:
            asyncio.create_task(do_authorize_then_start(self.charger, connector_id, id_tag))
        else:
            asyncio.create_task(do_start_charging(self.charger, connector_id, id_tag))
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id: int, **kwargs):
        from .session import do_stop_charging
        farm_metrics.record_message_received()
        for conn in self.charger.connectors.values():
            if conn.transaction_id == transaction_id:
                asyncio.create_task(do_stop_charging(self.charger, conn.connector_id, "Remote"))
                return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)

    @on(Action.get_configuration)
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
        return call_result.GetConfiguration(configuration_key=entries, unknown_key=unknown)

    @on(Action.change_configuration)
    async def on_change_config(self, key: str, value: str, **kwargs):
        farm_metrics.record_message_received()
        if key == "ConnectionTimeOut" and self.charger.quirks.reject_connection_timeout_change:
            return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
        if key in self.charger._config:
            if self.charger._config[key]["readonly"]:
                return call_result.ChangeConfiguration(status=ConfigurationStatus.rejected)
            self.charger._config[key]["value"] = value
            return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)
        return call_result.ChangeConfiguration(status=ConfigurationStatus.not_supported)

    @on(Action.set_charging_profile)
    async def on_set_charging_profile(self, connector_id: int, cs_charging_profiles: dict, **kwargs):
        from .session import apply_charging_profiles
        farm_metrics.record_message_received()
        prof = cs_charging_profiles
        sl = prof.get("stack_level", 0)
        prof["connector_id"] = connector_id
        self.charger._charging_profiles[sl] = prof
        conn = self.charger.connectors.get(connector_id)
        if conn and conn.charge_state:
            apply_charging_profiles(self.charger, conn)
        return call_result.SetChargingProfile(status="Accepted")

    @on(Action.clear_charging_profile)
    async def on_clear_charging_profile(self, **kwargs):
        farm_metrics.record_message_received()
        profile_id = kwargs.get("id")
        connector_id = kwargs.get("connector_id")
        stack_level = kwargs.get("stack_level")
        if stack_level is not None and stack_level in self.charger._charging_profiles:
            del self.charger._charging_profiles[stack_level]
            return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.accepted)
        elif connector_id is not None:
            to_remove = [sl for sl, p in self.charger._charging_profiles.items() if p.get("connector_id") == connector_id]
            for sl in to_remove:
                del self.charger._charging_profiles[sl]
            if to_remove:
                return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.accepted)
        elif not kwargs or profile_id is None:
            self.charger._charging_profiles.clear()
            return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.accepted)
        return call_result.ClearChargingProfile(status=ClearChargingProfileStatus.unknown)

    @on(Action.get_composite_schedule)
    async def on_get_composite_schedule(self, connector_id: int, duration: int, **kwargs):
        farm_metrics.record_message_received()
        periods = []
        for sl, prof in sorted(self.charger._charging_profiles.items()):
            schedule = prof.get("charging_schedule", {})
            for period in schedule.get("charging_schedule_period", []):
                periods.append(period)
        if periods:
            return call_result.GetCompositeSchedule(
                status="Accepted",
                connector_id=connector_id,
                schedule_start=_now_iso(),
                charging_schedule={
                    "duration": duration,
                    "charging_rate_unit": kwargs.get("charging_rate_unit", "W"),
                    "charging_schedule_period": periods,
                },
            )
        return call_result.GetCompositeSchedule(status="Rejected")

    @on(Action.reset)
    async def on_reset(self, type: str, **kwargs):
        from .session import do_stop_charging
        farm_metrics.record_message_received()
        for conn in self.charger.connectors.values():
            if conn.transaction_id is not None:
                await do_stop_charging(self.charger, conn.connector_id, "Reboot")
        if type == ResetType.hard:
            asyncio.create_task(self._hard_reset())
        return call_result.Reset(status=ResetStatus.accepted)

    async def _hard_reset(self):
        await asyncio.sleep(2)
        await self.charger.force_disconnect()

    @on(Action.trigger_message)
    async def on_trigger_message(self, requested_message: str, connector_id: int = 0, **kwargs):
        farm_metrics.record_message_received()
        if requested_message == "StatusNotification":
            conn = self.charger.connectors.get(connector_id)
            status = conn.status if conn else ChargePointStatus.available
            asyncio.create_task(self.charger._send_status(connector_id, status))
            return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        elif requested_message == "Heartbeat":
            asyncio.create_task(self.charger._call(call.Heartbeat()))
            return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        elif requested_message == "BootNotification":
            asyncio.create_task(self.charger._call(
                call.BootNotification(
                    charge_point_vendor=self.charger.profile.vendor,
                    charge_point_model=self.charger.profile.model,
                    charge_point_serial_number=f"{self.charger.cp_id}-SN",
                    firmware_version=self.charger.profile.firmware,
                )
            ))
            return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        elif requested_message == "MeterValues":
            from .messages import send_meter_values
            conn = self.charger.connectors.get(connector_id)
            if conn and conn.charge_state and conn.transaction_id:
                snap = conn.charge_state.tick(0)
                asyncio.create_task(send_meter_values(self.charger, connector_id, conn.transaction_id, snap))
                return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        elif requested_message == "FirmwareStatusNotification":
            if self.charger._firmware_status:
                asyncio.create_task(self.charger._call(
                    call.FirmwareStatusNotification(status=self.charger._firmware_status)
                ))
                return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        elif requested_message == "DiagnosticsStatusNotification":
            if self.charger._diagnostics_status:
                asyncio.create_task(self.charger._call(
                    call.DiagnosticsStatusNotification(status=self.charger._diagnostics_status)
                ))
                return call_result.TriggerMessage(status=TriggerMessageStatus.accepted)
        return call_result.TriggerMessage(status=TriggerMessageStatus.not_implemented)

    @on(Action.change_availability)
    async def on_change_availability(self, connector_id: int, type: str, **kwargs):
        farm_metrics.record_message_received()
        if connector_id == 0:
            for conn in self.charger.connectors.values():
                conn.available = (type == AvailabilityType.operative)
                if conn.transaction_id is None:
                    status = ChargePointStatus.available if conn.available else ChargePointStatus.unavailable
                    asyncio.create_task(self.charger._send_status(conn.connector_id, status))
            return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)
        conn = self.charger.connectors.get(connector_id)
        if conn:
            conn.available = (type == AvailabilityType.operative)
            if conn.transaction_id is None:
                status = ChargePointStatus.available if conn.available else ChargePointStatus.unavailable
                asyncio.create_task(self.charger._send_status(connector_id, status))
            return call_result.ChangeAvailability(
                status=AvailabilityStatus.accepted if conn.transaction_id is None else AvailabilityStatus.scheduled
            )
        return call_result.ChangeAvailability(status=AvailabilityStatus.rejected)

    @on(Action.reserve_now)
    async def on_reserve_now(self, connector_id: int, expiry_date: str, id_tag: str,
                              reservation_id: int, **kwargs):
        farm_metrics.record_message_received()
        conn = self.charger.connectors.get(connector_id)
        if not conn:
            return call_result.ReserveNow(status=ReservationStatus.rejected)
        if conn.transaction_id is not None:
            return call_result.ReserveNow(status=ReservationStatus.occupied)
        if not conn.available:
            return call_result.ReserveNow(status=ReservationStatus.unavailable)
        conn.reservation_id = reservation_id
        conn.reserved_id_tag = id_tag
        asyncio.create_task(self.charger._send_status(connector_id, ChargePointStatus.reserved))
        return call_result.ReserveNow(status=ReservationStatus.accepted)

    @on(Action.cancel_reservation)
    async def on_cancel_reservation(self, reservation_id: int, **kwargs):
        farm_metrics.record_message_received()
        for conn in self.charger.connectors.values():
            if conn.reservation_id == reservation_id:
                conn.reservation_id = None
                conn.reserved_id_tag = None
                asyncio.create_task(self.charger._send_status(conn.connector_id, ChargePointStatus.available))
                return call_result.CancelReservation(status="Accepted")
        return call_result.CancelReservation(status="Rejected")

    @on(Action.update_firmware)
    async def on_update_firmware(self, location: str, retrieve_date: str, **kwargs):
        from .connection import simulate_firmware_update
        farm_metrics.record_message_received()
        if self.charger._firmware_task:
            self.charger._firmware_task.cancel()
        self.charger._firmware_task = asyncio.create_task(
            simulate_firmware_update(self.charger, location, retrieve_date)
        )
        return call_result.UpdateFirmware()

    @on(Action.get_diagnostics)
    async def on_get_diagnostics(self, location: str, **kwargs):
        farm_metrics.record_message_received()
        filename = f"diag-{self.charger.cp_id}-{int(time.time())}.txt"
        asyncio.create_task(self._simulate_diagnostics())
        return call_result.GetDiagnostics(file_name=filename)

    async def _simulate_diagnostics(self):
        try:
            self.charger._diagnostics_status = DiagnosticsStatus.uploading
            await self.charger._call(call.DiagnosticsStatusNotification(status=DiagnosticsStatus.uploading))
            await asyncio.sleep(random.uniform(3, 8))
            self.charger._diagnostics_status = DiagnosticsStatus.uploaded
            await self.charger._call(call.DiagnosticsStatusNotification(status=DiagnosticsStatus.uploaded))
        except asyncio.CancelledError:
            pass

    @on(Action.send_local_list)
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
        return call_result.SendLocalList(status=UpdateStatus.accepted)

    @on(Action.data_transfer)
    async def on_data_transfer(self, vendor_id: str, **kwargs):
        farm_metrics.record_message_received()
        message_id = kwargs.get("message_id", "")
        farm_metrics.log_event("info", self.charger.cp_id, f"DataTransfer vendor={vendor_id} msg={message_id}")
        return call_result.DataTransfer(status=DataTransferStatus.accepted, data="{}")
