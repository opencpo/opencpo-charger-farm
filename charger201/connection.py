"""
OCPP 2.0.1 Virtual Charger — connection loop, boot, cleanup.
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import websockets
from ocpp.v201 import call as call201
from ocpp.v201.enums import BootReasonType, FirmwareStatusType, RegistrationStatusType

from metrics import farm_metrics

if TYPE_CHECKING:
    from .core import VirtualCharger201

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_connection_loop(charger: "VirtualCharger201") -> None:
    charger._running = True
    charger._shutdown_event.clear()
    farm_metrics.log_event("info", charger.cp_id, f"Charger 2.0.1 starting (profile={charger.profile.name})")

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
    farm_metrics.log_event("info", charger.cp_id, "Charger 2.0.1 stopped")


async def connect_and_run(charger: "VirtualCharger201") -> None:
    from .ocpp_handler import _ChargePointHandler201

    url = charger.ws_url.rstrip("/") + f"/{charger.cp_id}"

    async with websockets.connect(
        url,
        subprotocols=["ocpp2.0.1"],
        ping_interval=20 if not charger.quirks.websocket_ping_interval_zero else None,
        ping_timeout=30,
        close_timeout=10,
        max_size=2**20,
    ) as ws:
        charger._ws = ws
        charger._cp = _ChargePointHandler201(charger.cp_id, ws, charger)
        charger._connected = True
        charger.network.go_online()
        farm_metrics.record_connection()
        farm_metrics.log_event("info", charger.cp_id, "Connected (2.0.1)")

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


async def boot(charger: "VirtualCharger201") -> None:
    t0 = time.monotonic()
    resp = await charger._call(
        call201.BootNotificationPayload(
            charging_station={
                "model": charger.profile.model,
                "vendor_name": charger.profile.vendor,
                "serial_number": f"{charger.cp_id}-SN",
                "firmware_version": charger.profile.firmware,
            },
            reason=BootReasonType.power_up,
        )
    )
    farm_metrics.record_latency((time.monotonic() - t0) * 1000)

    if resp and resp.status == RegistrationStatusType.accepted:
        if resp.interval > 0:
            charger._variables["OCPPCommCtrlr.HeartbeatInterval"]["value"] = str(resp.interval)
        farm_metrics.log_event("info", charger.cp_id, f"Boot 2.0.1 accepted (interval={resp.interval}s)")
    else:
        farm_metrics.log_event("warning", charger.cp_id,
                               f"Boot 2.0.1 status: {resp.status if resp else 'no response'}")

    for evse in charger.evses.values():
        await charger._send_status(evse.evse_id, evse.connector_id, evse.status)

    charger._heartbeat_task = asyncio.create_task(heartbeat_loop(charger))

    await charger._call(
        call201.SecurityEventNotificationPayload(
            type="StartupOfTheDevice",
            timestamp=_now_iso(),
        )
    )


async def cleanup(charger: "VirtualCharger201") -> None:
    if charger._heartbeat_task:
        charger._heartbeat_task.cancel()
        charger._heartbeat_task = None
    for evse in charger.evses.values():
        if evse.meter_task:
            evse.meter_task.cancel()
            evse.meter_task = None
    if charger._firmware_task:
        charger._firmware_task.cancel()
        charger._firmware_task = None
    charger._connected = False


async def heartbeat_loop(charger: "VirtualCharger201") -> None:
    try:
        while charger._connected:
            interval = int(charger._variables.get("OCPPCommCtrlr.HeartbeatInterval", {}).get("value", "30"))
            await asyncio.sleep(interval)
            if not charger._connected:
                break
            await charger._call(call201.HeartbeatPayload())
    except asyncio.CancelledError:
        pass


async def replay_offline_buffer(charger: "VirtualCharger201") -> None:
    queued = charger.network.offline_buffer.drain_meter_values()
    if not queued:
        return
    farm_metrics.log_event("info", charger.cp_id, f"Replaying {len(queued)} offline messages")
    for msg in queued:
        await asyncio.sleep(0.1)
        farm_metrics.log_event("info", charger.cp_id, f"Replayed offline: {msg['type']}")


async def simulate_firmware_update(charger: "VirtualCharger201", location: str) -> None:
    try:
        for status in [FirmwareStatusType.downloading, FirmwareStatusType.downloaded,
                       FirmwareStatusType.installing]:
            charger._firmware_status = status
            await charger._call(call201.FirmwareStatusNotificationPayload(status=status))
            await asyncio.sleep(random.uniform(3, 10))
        if random.random() < 0.9:
            charger._firmware_status = FirmwareStatusType.installed
            await charger._call(call201.FirmwareStatusNotificationPayload(status=FirmwareStatusType.installed))
        else:
            charger._firmware_status = FirmwareStatusType.installation_failed
            await charger._call(call201.FirmwareStatusNotificationPayload(status=FirmwareStatusType.installation_failed))
    except asyncio.CancelledError:
        pass
