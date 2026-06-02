"""
OCPP 1.6j Virtual Charger — connection loop, boot, cleanup.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Optional

import websockets
from ocpp.v16 import call
from ocpp.v16.enums import (
    ChargePointErrorCode,
    ChargePointStatus,
    FirmwareStatus,
    RegistrationStatus,
)

from metrics import farm_metrics

if TYPE_CHECKING:
    from .core import VirtualCharger16

log = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def run_connection_loop(charger: "VirtualCharger16") -> None:
    """Main connection loop with exponential backoff reconnect."""
    charger._running = True
    charger._shutdown_event.clear()
    farm_metrics.log_event("info", charger.cp_id, f"Charger starting (profile={charger.profile.name})")

    delay = 5
    max_delay = 60

    while charger._running and not charger._shutdown_event.is_set():
        try:
            await connect_and_run(charger)
            if charger._shutdown_event.is_set():
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
            farm_metrics.log_event("warning", charger.cp_id, f"Connection lost: {e}")
            charger._connected = False
            charger.network.go_offline()
        except Exception as e:
            farm_metrics.record_error()
            farm_metrics.log_event("error", charger.cp_id, f"Unexpected error: {e}")
            charger._connected = False

        if charger._running and not charger._shutdown_event.is_set():
            farm_metrics.log_event("info", charger.cp_id, f"Reconnecting in {delay}s...")
            try:
                await asyncio.wait_for(charger._shutdown_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, max_delay)

    charger._running = False
    farm_metrics.log_event("info", charger.cp_id, "Charger stopped")


async def connect_and_run(charger: "VirtualCharger16") -> None:
    """Single connection attempt."""
    from .ocpp_handler import _ChargePointHandler

    url = charger.ws_url.rstrip("/") + f"/{charger.cp_id}"
    ping_interval = 20
    if charger.quirks.websocket_ping_interval_zero:
        ping_interval = None

    async with websockets.connect(
        url,
        subprotocols=["ocpp1.6"],
        ping_interval=ping_interval,
        ping_timeout=30,
        close_timeout=10,
        max_size=2**20,
    ) as ws:
        charger._ws = ws
        charger._cp = _ChargePointHandler(charger.cp_id, ws, charger)
        charger._connected = True
        charger.network.go_online()
        farm_metrics.record_connection()
        farm_metrics.log_event("info", charger.cp_id, "Connected")

        if charger.quirks.stop_transaction_on_reconnect:
            await replay_reconnect_stops(charger)

        await replay_offline_buffer(charger)
        await boot(charger)

        shutdown_task = asyncio.create_task(charger._shutdown_event.wait())
        handler_task = asyncio.create_task(charger._cp.start())

        done, pending = await asyncio.wait(
            [handler_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        await cleanup(charger)
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


async def boot(charger: "VirtualCharger16") -> None:
    """Run OCPP boot sequence."""
    t0 = time.monotonic()
    resp = await charger._call(
        call.BootNotification(
            charge_point_vendor=charger.profile.vendor,
            charge_point_model=charger.profile.model,
            charge_point_serial_number=f"{charger.cp_id}-SN",
            firmware_version=charger.profile.firmware,
        )
    )
    farm_metrics.record_latency((time.monotonic() - t0) * 1000)

    if resp and resp.status == RegistrationStatus.accepted:
        if resp.interval > 0:
            charger._config["HeartbeatInterval"]["value"] = str(resp.interval)
        farm_metrics.log_event("info", charger.cp_id, f"Boot accepted (interval={resp.interval}s)")
    else:
        farm_metrics.log_event("warning", charger.cp_id,
                               f"Boot status: {resp.status if resp else 'no response'}")

    for conn_id in range(0, charger.profile.num_connectors + 1):
        status = ChargePointStatus.available
        if conn_id > 0 and not charger.connectors[conn_id].available:
            status = ChargePointStatus.unavailable
        await charger._send_status(conn_id, status)

    charger._heartbeat_task = asyncio.create_task(heartbeat_loop(charger))


async def cleanup(charger: "VirtualCharger16") -> None:
    """Cancel all internal tasks."""
    if charger._heartbeat_task:
        charger._heartbeat_task.cancel()
        charger._heartbeat_task = None
    for conn in charger.connectors.values():
        if conn.meter_task:
            conn.meter_task.cancel()
            conn.meter_task = None
    if charger._firmware_task:
        charger._firmware_task.cancel()
        charger._firmware_task = None
    charger._connected = False


async def heartbeat_loop(charger: "VirtualCharger16") -> None:
    try:
        while charger._connected:
            interval = int(charger._config["HeartbeatInterval"]["value"])
            await asyncio.sleep(interval)
            if not charger._connected:
                break
            t0 = time.monotonic()
            resp = await charger._call(call.Heartbeat())
            if resp:
                farm_metrics.record_latency((time.monotonic() - t0) * 1000)
    except asyncio.CancelledError:
        pass


async def replay_offline_buffer(charger: "VirtualCharger16") -> None:
    from ocpp.v16 import call
    from ocpp.v16.enums import Measurand, UnitOfMeasure

    queued = charger.network.offline_buffer.drain_meter_values()
    if not queued:
        return
    farm_metrics.log_event("info", charger.cp_id, f"Replaying {len(queued)} offline messages")
    for msg in queued:
        if msg["type"] == "MeterValues":
            snap_dict = msg["snapshot"]
            sampled = [
                {"value": str(snap_dict["energy_wh"]), "measurand": Measurand.energy_active_import_register, "unit": UnitOfMeasure.wh},
                {"value": str(snap_dict["power_w"]), "measurand": Measurand.power_active_import, "unit": UnitOfMeasure.w},
                {"value": str(snap_dict["soc"]), "measurand": Measurand.soc, "unit": UnitOfMeasure.percent},
            ]
            await charger._call(
                call.MeterValues(
                    connector_id=msg["connector_id"],
                    transaction_id=msg["transaction_id"],
                    meter_value=[{"timestamp": msg["timestamp"], "sampled_value": sampled}],
                )
            )
        elif msg["type"] == "StopTransaction":
            await charger._call(
                call.StopTransaction(
                    meter_stop=msg["meter_stop"],
                    timestamp=msg["timestamp"],
                    transaction_id=msg["transaction_id"],
                    reason=msg["reason"],
                )
            )
        await asyncio.sleep(0.1)


async def replay_reconnect_stops(charger: "VirtualCharger16") -> None:
    """MAXPOWER quirk: send StopTransaction reason=Other for sessions active during disconnect."""
    from ocpp.v16 import call

    for conn in charger.connectors.values():
        if conn.transaction_id is not None:
            meter = int(conn.charge_state.meter_wh) if conn.charge_state else 0
            await charger._call(
                call.StopTransaction(
                    meter_stop=meter,
                    timestamp=_now_iso(),
                    transaction_id=conn.transaction_id,
                    reason="Other",
                )
            )
            conn.transaction_id = None
            conn.charge_state = None
            farm_metrics.log_event("info", charger.cp_id,
                                   f"Reconnect StopTransaction (quirk) conn={conn.connector_id}")


async def simulate_firmware_update(charger: "VirtualCharger16", location: str, retrieve_date: str) -> None:
    """Simulate firmware update lifecycle."""
    try:
        await _send_firmware_status(charger, FirmwareStatus.downloading)
        await asyncio.sleep(random.uniform(5, 15))
        await _send_firmware_status(charger, FirmwareStatus.downloaded)
        await asyncio.sleep(2)
        await _send_firmware_status(charger, FirmwareStatus.installing)
        await asyncio.sleep(random.uniform(10, 30))
        if random.random() < 0.9:
            await _send_firmware_status(charger, FirmwareStatus.installed)
            farm_metrics.log_event("info", charger.cp_id, "Firmware update installed")
        else:
            await _send_firmware_status(charger, FirmwareStatus.installation_failed)
            farm_metrics.log_event("error", charger.cp_id, "Firmware update failed")
    except asyncio.CancelledError:
        pass


async def _send_firmware_status(charger: "VirtualCharger16", status: str) -> None:
    from ocpp.v16 import call
    charger._firmware_status = status
    await charger._call(call.FirmwareStatusNotification(status=status))
