"""
OCPP 2.0.1 Virtual Charger — session management (start/stop/V2G/PnC).
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from ocpp.v201 import call as call201
from ocpp.v201.enums import (
    ChargingStateType,
    ConnectorStatusType,
    IdTokenType,
    TransactionEventType,
)

from metrics import farm_metrics
from physics import ChargeState, EnvironmentConditions, PowerDirection, V2GConfig, random_start_soc
from pnc import generate_emaid, generate_exi_cert_request

if TYPE_CHECKING:
    from .core import VirtualCharger201, EVSEState

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def send_transaction_event(charger: "VirtualCharger201", event_type: str,
                                  evse: "EVSEState", trigger: str = "Authorized",
                                  meter_values: list = None) -> Optional[object]:
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
    return await charger._call(call201.TransactionEventPayload(**payload_dict))


async def start_charging(charger: "VirtualCharger201", evse_id: int = 1,
                          id_token_value: str = "VIRTUAL-TAG") -> bool:
    evse = charger.evses.get(evse_id)
    if not evse or evse.transaction_id is not None or not evse.available:
        return False
    asyncio.create_task(do_start_charging(charger, evse_id, id_token_value))
    return True


async def do_start_charging(charger: "VirtualCharger201", evse_id: int, id_token_value: str) -> None:
    evse = charger.evses[evse_id]

    await charger._send_status(evse_id, evse.connector_id, ConnectorStatusType.occupied)

    txn_id = charger._next_txn_id()
    evse.transaction_id = txn_id
    evse.id_token = {"id_token": id_token_value, "type": IdTokenType.iso14443}
    evse.charging_state = ChargingStateType.ev_connected
    evse.seq_no = 0

    await send_transaction_event(charger, TransactionEventType.started, evse, trigger="Authorized")

    await asyncio.sleep(random.uniform(1, 3))

    soc = random_start_soc()
    meter_start = charger._cumulative_wh.get(evse_id, 0)
    env_conds = EnvironmentConditions(ambient_temp_c=charger.env_sim.site.ambient_temp_c)
    evse.charge_state = ChargeState(soc=soc, meter_wh=float(meter_start), max_kw=charger.profile.max_kw)
    evse.charge_state.env = env_conds
    evse.charge_state.direction = PowerDirection.CHARGING
    evse.charging_state = ChargingStateType.charging

    await send_transaction_event(charger, TransactionEventType.updated, evse, trigger="ChargingStateChanged")

    farm_metrics.record_session_started()
    farm_metrics.log_event("info", charger.cp_id, f"Started txn={txn_id} evse={evse_id} soc={soc:.1f}%")

    evse.meter_task = asyncio.create_task(meter_loop(charger, evse_id))


async def stop_charging(charger: "VirtualCharger201", evse_id: int = 1, reason: str = "Remote") -> bool:
    evse = charger.evses.get(evse_id)
    if not evse or evse.transaction_id is None:
        return False
    asyncio.create_task(do_stop_charging(charger, evse_id, reason))
    return True


async def do_stop_charging(charger: "VirtualCharger201", evse_id: int, reason: str = "Remote") -> None:
    from .messages import build_meter_values
    evse = charger.evses[evse_id]
    if evse.transaction_id is None:
        return

    if evse.meter_task:
        evse.meter_task.cancel()
        try:
            await evse.meter_task
        except asyncio.CancelledError:
            pass
        evse.meter_task = None

    meter_stop = int(evse.charge_state.meter_wh) if evse.charge_state else int(charger._cumulative_wh.get(evse_id, 0))
    charger._cumulative_wh[evse_id] = float(meter_stop)
    if evse.charge_state:
        charger._cumulative_export_wh[evse_id] = evse.charge_state.discharge_meter_wh

    evse.charging_state = "Idle"

    mv = build_meter_values(evse) if evse.charge_state else None
    await send_transaction_event(
        charger, TransactionEventType.ended, evse,
        trigger="StopAuthorized" if reason == "Remote" else "EVDeparted",
        meter_values=mv,
    )

    farm_metrics.record_session_ended()
    farm_metrics.log_event("info", charger.cp_id, f"Stopped txn={evse.transaction_id} evse={evse_id}")

    evse.transaction_id = None
    evse.id_token = None
    evse.charge_state = None

    await charger._send_status(evse_id, evse.connector_id, ConnectorStatusType.available)


async def start_v2g(charger: "VirtualCharger201", evse_id: int = 1,
                    max_discharge_kw: float = 50.0, min_soc: float = 20.0) -> bool:
    evse = charger.evses.get(evse_id)
    if not evse or not evse.charge_state or evse.transaction_id is None:
        return False
    evse.charge_state.v2g = V2GConfig(enabled=True, max_discharge_kw=max_discharge_kw, min_soc_floor=min_soc)
    evse.charge_state.direction = PowerDirection.DISCHARGING
    evse.charging_state = "Discharging"
    await send_transaction_event(charger, TransactionEventType.updated, evse, trigger="ChargingStateChanged")
    farm_metrics.log_event("info", charger.cp_id, f"V2G started evse={evse_id}")
    return True


async def stop_v2g(charger: "VirtualCharger201", evse_id: int = 1) -> bool:
    evse = charger.evses.get(evse_id)
    if not evse or not evse.charge_state:
        return False
    evse.charge_state.v2g.enabled = False
    evse.charge_state.direction = PowerDirection.CHARGING
    evse.charging_state = ChargingStateType.charging
    farm_metrics.log_event("info", charger.cp_id, f"V2G stopped evse={evse_id}")
    return True


async def trigger_pnc(charger: "VirtualCharger201", evse_id: int = 1) -> bool:
    if not charger.pnc_config.enabled:
        return False
    charger.pnc_config.contract_id_counter += 1
    emaid = generate_emaid(charger.pnc_config.emaid_prefix, charger.pnc_config.contract_id_counter)
    exi = generate_exi_cert_request(emaid)
    await asyncio.sleep(charger.pnc_config.tls_handshake_delay_sec)
    await charger._call(
        call201.Get15118EVCertificatePayload(
            iso15118_schema_version="urn:iso:15118:2:2013:MsgDef" if not charger.pnc_config.iso15118_20 else "urn:iso:std:iso:15118:-20:DC",
            action="Install",
            exi_request=exi,
        )
    )
    await start_charging(charger, evse_id, emaid)
    farm_metrics.log_event("info", charger.cp_id, f"PnC 2.0.1 flow started emaid={emaid}")
    return True


async def meter_loop(charger: "VirtualCharger201", evse_id: int) -> None:
    from .messages import build_meter_values
    evse = charger.evses[evse_id]
    try:
        while evse.transaction_id is not None and evse.charge_state is not None:
            interval = int(charger._variables.get("SampledDataCtrlr.TxUpdatedInterval", {}).get("value", "30"))
            await asyncio.sleep(interval)

            if evse.transaction_id is None or evse.charge_state is None:
                break

            fault = charger.env_sim.has_session_stopping_fault(charger.cp_id, evse_id)
            if fault:
                farm_metrics.log_event("warning", charger.cp_id, f"Fault stops session: {fault.fault_type.value}")
                await do_stop_charging(charger, evse_id, "Other")
                return

            evse.charge_state.hardware_derating = charger.env_sim.get_derating_factor(charger.cp_id)
            evse.charge_state.env.ambient_temp_c = charger.env_sim.site.ambient_temp_c

            snap = evse.charge_state.tick(float(interval))
            charger._cumulative_wh[evse_id] = evse.charge_state.meter_wh

            if charger._connected:
                mv = build_meter_values(evse)
                await send_transaction_event(charger, TransactionEventType.updated, evse,
                                             trigger="MeterValuePeriodic", meter_values=mv)
            else:
                charger.network.offline_buffer.queue_meter_value({
                    "type": "TransactionEvent",
                    "evse_id": evse_id,
                    "transaction_id": evse.transaction_id,
                    "timestamp": _now_iso(),
                    "snapshot": snap.as_dict(),
                })

            if evse.charge_state.is_full:
                farm_metrics.log_event("info", charger.cp_id, f"SoC 100% evse={evse_id}")
                await do_stop_charging(charger, evse_id, "Local")
                return

    except asyncio.CancelledError:
        pass
