"""
OCPP 1.6j Virtual Charger — session management (start/stop/V2G/PnC).
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from ocpp.v16 import call
from ocpp.v16.enums import ChargePointStatus

from metrics import farm_metrics
from physics import ChargeState, ChargingProfileLimit, EnvironmentConditions, PowerDirection, V2GConfig, random_start_soc
from pnc import generate_emaid, generate_exi_cert_request

if TYPE_CHECKING:
    from .core import VirtualCharger16

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def start_charging(charger: "VirtualCharger16", connector_id: int = 1, id_tag: str = "VIRTUAL-TAG") -> bool:
    """Externally triggered charge start."""
    conn = charger.connectors.get(connector_id)
    if not conn or conn.transaction_id is not None:
        return False
    if not conn.available:
        return False
    asyncio.create_task(do_start_charging(charger, connector_id, id_tag))
    return True


async def do_authorize_then_start(charger: "VirtualCharger16", connector_id: int, id_tag: str) -> None:
    """Maxpower behavior: send Authorize AFTER accepting RemoteStart, BEFORE StartTransaction."""
    try:
        result = await charger._call(call.AuthorizePayload(id_tag=id_tag))
        status = result.id_tag_info.get("status", "Accepted") if hasattr(result, "id_tag_info") else "Accepted"
        if status != "Accepted":
            log.warning(f"[{charger.cp_id}] Authorize rejected for {id_tag}: {status}")
            return
    except Exception as e:
        log.warning(f"[{charger.cp_id}] Authorize failed: {e}")
    await do_start_charging(charger, connector_id, id_tag)


async def do_start_charging(charger: "VirtualCharger16", connector_id: int, id_tag: str) -> None:
    conn = charger.connectors[connector_id]

    if conn.reservation_id is not None and conn.reserved_id_tag != id_tag:
        farm_metrics.log_event("warning", charger.cp_id,
                               f"Connector {connector_id} reserved for {conn.reserved_id_tag}")
        return

    await charger._send_status(connector_id, ChargePointStatus.preparing)
    await asyncio.sleep(random.uniform(1, 3))

    if charger.pnc_config.enabled and id_tag.startswith("EMAID:"):
        id_tag = id_tag.replace("EMAID:", "")

    soc = random_start_soc()
    meter_start = int(charger._cumulative_wh.get(connector_id, 0))

    resp = await charger._call(
        call.StartTransactionPayload(
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=meter_start,
            timestamp=_now_iso(),
        )
    )
    if not resp:
        await charger._send_status(connector_id, ChargePointStatus.available)
        return

    txn_id = resp.transaction_id
    conn.transaction_id = txn_id
    conn.id_tag = id_tag

    env_conds = EnvironmentConditions(ambient_temp_c=charger.env_sim.site.ambient_temp_c)
    conn.charge_state = ChargeState(
        soc=soc,
        meter_wh=float(meter_start),
        max_kw=charger.profile.max_kw,
    )
    conn.charge_state.env = env_conds
    conn.charge_state.direction = PowerDirection.CHARGING

    apply_charging_profiles(charger, conn)

    conn.reservation_id = None
    conn.reserved_id_tag = None

    farm_metrics.record_session_started()
    farm_metrics.log_event("info", charger.cp_id, f"Started txn={txn_id} conn={connector_id} soc={soc:.1f}%")

    await charger._send_status(connector_id, ChargePointStatus.charging)
    conn.meter_task = asyncio.create_task(meter_loop(charger, connector_id))


async def stop_charging(charger: "VirtualCharger16", connector_id: int = 1, reason: str = "Remote") -> bool:
    """Externally triggered charge stop."""
    conn = charger.connectors.get(connector_id)
    if not conn or conn.transaction_id is None:
        return False
    asyncio.create_task(do_stop_charging(charger, connector_id, reason))
    return True


async def do_stop_charging(charger: "VirtualCharger16", connector_id: int, reason: str = "Remote") -> None:
    conn = charger.connectors[connector_id]
    if conn.transaction_id is None:
        return

    if conn.meter_task:
        conn.meter_task.cancel()
        try:
            await conn.meter_task
        except asyncio.CancelledError:
            pass
        conn.meter_task = None

    meter_stop = int(conn.charge_state.meter_wh) if conn.charge_state else int(charger._cumulative_wh.get(connector_id, 0))
    charger._cumulative_wh[connector_id] = float(meter_stop)
    if conn.charge_state:
        charger._cumulative_export_wh[connector_id] = conn.charge_state.discharge_meter_wh

    if charger._connected:
        await charger._call(
            call.StopTransactionPayload(
                meter_stop=meter_stop,
                timestamp=_now_iso(),
                transaction_id=conn.transaction_id,
                reason=reason,
            )
        )
    else:
        charger.network.offline_buffer.queue_meter_value({
            "type": "StopTransaction",
            "meter_stop": meter_stop,
            "timestamp": _now_iso(),
            "transaction_id": conn.transaction_id,
            "reason": reason,
        })

    farm_metrics.record_session_ended()
    farm_metrics.log_event("info", charger.cp_id,
                           f"Stopped txn={conn.transaction_id} conn={connector_id} reason={reason}")

    conn.transaction_id = None
    conn.id_tag = None
    conn.charge_state = None

    await charger._send_status(connector_id, ChargePointStatus.finishing)
    await asyncio.sleep(random.uniform(2, 5))
    status = ChargePointStatus.available if conn.available else ChargePointStatus.unavailable
    await charger._send_status(connector_id, status)


async def start_v2g(charger: "VirtualCharger16", connector_id: int = 1,
                    max_discharge_kw: float = 50.0, min_soc: float = 20.0) -> bool:
    conn = charger.connectors.get(connector_id)
    if not conn or not conn.charge_state or conn.transaction_id is None:
        return False
    conn.charge_state.v2g = V2GConfig(enabled=True, max_discharge_kw=max_discharge_kw, min_soc_floor=min_soc)
    conn.charge_state.direction = PowerDirection.DISCHARGING
    farm_metrics.log_event("info", charger.cp_id, f"V2G started conn={connector_id} max={max_discharge_kw}kW")
    return True


async def stop_v2g(charger: "VirtualCharger16", connector_id: int = 1) -> bool:
    conn = charger.connectors.get(connector_id)
    if not conn or not conn.charge_state:
        return False
    conn.charge_state.v2g.enabled = False
    conn.charge_state.direction = PowerDirection.CHARGING
    farm_metrics.log_event("info", charger.cp_id, f"V2G stopped conn={connector_id}")
    return True


async def trigger_pnc(charger: "VirtualCharger16", connector_id: int = 1) -> bool:
    """Trigger Plug & Charge flow (1.6j: DataTransfer + eMAID id_tag)."""
    if not charger.pnc_config.enabled:
        return False

    charger.pnc_config.contract_id_counter += 1
    emaid = generate_emaid(charger.pnc_config.emaid_prefix, charger.pnc_config.contract_id_counter)
    exi = generate_exi_cert_request(emaid)

    await asyncio.sleep(charger.pnc_config.tls_handshake_delay_sec)

    await charger._call(
        call.DataTransferPayload(
            vendor_id="org.openchargealliance.iso15118pnc",
            message_id="Authorize",
            data=json.dumps({"eMAID": emaid, "exiRequest": exi}),
        )
    )

    await start_charging(charger, connector_id, f"EMAID:{emaid}")
    farm_metrics.log_event("info", charger.cp_id, f"PnC flow started emaid={emaid}")
    return True


async def meter_loop(charger: "VirtualCharger16", connector_id: int) -> None:
    from .messages import send_meter_values
    conn = charger.connectors[connector_id]
    try:
        while conn.transaction_id is not None and conn.charge_state is not None:
            interval = int(charger._config["MeterValueSampleInterval"]["value"])
            await asyncio.sleep(interval)

            if conn.transaction_id is None or conn.charge_state is None:
                break

            fault = charger.env_sim.has_session_stopping_fault(charger.cp_id, connector_id)
            if fault:
                farm_metrics.log_event("warning", charger.cp_id,
                                       f"Fault stops session: {fault.fault_type.value}")
                await do_stop_charging(charger, connector_id, "Other")
                return

            conn.charge_state.hardware_derating = charger.env_sim.get_derating_factor(charger.cp_id)
            conn.charge_state.env.ambient_temp_c = charger.env_sim.site.ambient_temp_c

            snap = conn.charge_state.tick(float(interval))
            charger._cumulative_wh[connector_id] = conn.charge_state.meter_wh

            if charger._connected:
                await send_meter_values(charger, connector_id, conn.transaction_id, snap)
            else:
                charger.network.offline_buffer.queue_meter_value({
                    "type": "MeterValues",
                    "connector_id": connector_id,
                    "transaction_id": conn.transaction_id,
                    "timestamp": _now_iso(),
                    "snapshot": snap.as_dict(),
                })

            if conn.charge_state.is_full:
                farm_metrics.log_event("info", charger.cp_id, f"SoC 100% conn={connector_id}")
                await do_stop_charging(charger, connector_id, "Local")
                return

            if conn.charge_state.direction == PowerDirection.IDLE:
                farm_metrics.log_event("info", charger.cp_id, f"V2G SoC floor reached conn={connector_id}")

    except asyncio.CancelledError:
        pass


def apply_charging_profiles(charger: "VirtualCharger16", conn) -> None:
    """Apply stored charging profiles to a connector's charge state."""
    if conn.charge_state is None:
        return
    conn.charge_state.charging_profiles.clear()
    for sl, prof in charger._charging_profiles.items():
        if prof.get("connector_id", 0) in (0, conn.connector_id):
            schedule = prof.get("charging_schedule", {})
            periods = schedule.get("charging_schedule_period", [])
            if periods:
                for period in periods:
                    conn.charge_state.charging_profiles.append(
                        ChargingProfileLimit(
                            limit_kw=period.get("limit", charger.profile.max_kw),
                            stack_level=prof.get("stack_level", 0),
                            start_time=period.get("start_period", 0),
                        )
                    )
