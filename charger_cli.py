"""Standalone charger CLI — run a single virtual charger from the command line."""
import asyncio
import os
import signal
import sys

from charger import VirtualCharger, ChargingSession
from profiles import get_profile, MAXPOWER_QUIRKS
from environment import EnvironmentSimulator
from network import NetworkSimulator, NetworkConfig

def handle_signal(*_):
    log.info("Signal received, shutting down")
    shutdown_event.set()


async def connect_and_run():
    """Connect to OCPP server and run until disconnected or shutdown."""
    url = OCPP_URL.replace("{cp_id}", CP_ID)
    log.info("Connecting to %s as %s", url, CP_ID)

    async with websockets.connect(
        url,
        subprotocols=["ocpp1.6"],
        ping_interval=20,
        ping_timeout=30,
        close_timeout=10,
    ) as ws:
        charger = VirtualCharger(CP_ID, ws)
        log.info("Connected to %s", url)

        # Run boot + message handler concurrently
        boot_task = asyncio.create_task(charger.boot())
        handler_task = asyncio.create_task(charger.start())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        # Wait for boot to complete first (don't treat boot finishing as exit)
        try:
            await boot_task
        except Exception as e:
            log.error("Boot failed: %s", e)

        # Now wait for either handler (connection lost) or shutdown
        done, pending = await asyncio.wait(
            [handler_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        await charger.cleanup()

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


async def main():
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    delay = RECONNECT_DELAY
    max_delay = 60

    while not shutdown_event.is_set():
        try:
            await connect_and_run()
            if shutdown_event.is_set():
                break
            delay = RECONNECT_DELAY  # Reset on clean disconnect
        except (
            websockets.exceptions.ConnectionClosed,
            websockets.exceptions.InvalidURI,
            websockets.exceptions.InvalidHandshake,
            ConnectionRefusedError,
            OSError,
        ) as e:
            log.warning("Connection lost: %s", e)
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)

        if not shutdown_event.is_set():
            log.info("Reconnecting in %ds...", delay)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, max_delay)

    log.info("Charger %s stopped", CP_ID)


if __name__ == "__main__":
    asyncio.run(main())

