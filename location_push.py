"""
location_push.py — push location metadata to the Core API after charger boot.

This is a Farm → Core API call. No direct DB or Redis access.
"""

import asyncio
import logging

import httpx

from locations import get_location

log = logging.getLogger(__name__)


async def push_location(
    cp_id: str,
    location_index: int,
    api_url: str,
    api_key: str,
    log_event_fn=None,
) -> None:
    """Push location + simulated=false to Core API for a newly booted charger.

    Waits 5s for BootNotification to complete, retries once on 404 (charger
    not yet registered). Logs errors and returns silently — a failed metadata
    push must never crash a charger task.

    Args:
        cp_id:          Charge point ID.
        location_index: Index into the location pool (cycled via modulo).
        api_url:        Core API base URL (e.g. "http://localhost:8000").
        api_key:        Management API key (sent as X-API-Key header).
        log_event_fn:   Optional callable(level, source, msg) for farm metrics.
    """
    if not api_url or not api_key:
        log.debug("push_location: CPO_API_URL or CPO_API_KEY not set, skipping %s", cp_id)
        return

    loc = get_location(location_index)
    payload = {
        "display_name": loc["display_name"],
        "address": loc["address"],
        "city": loc["city"],
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
        "simulated": False,
    }
    url = f"{api_url.rstrip('/')}/api/v1/chargers/{cp_id}"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    def _log(level: str, msg: str) -> None:
        getattr(log, level)(msg)
        if log_event_fn:
            log_event_fn(level, cp_id, msg)

    # Give the charger time to complete BootNotification so Core has the row.
    await asyncio.sleep(5)

    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.put(url, json=payload, headers=headers)

            if resp.status_code == 200:
                _log("info", f"Location set: {loc['display_name']} ({loc['latitude']}, {loc['longitude']})")
                return

            if resp.status_code == 404 and attempt == 1:
                _log("warning", f"Charger not in Core yet, retrying in 10s")
                await asyncio.sleep(10)
                continue

            _log("error", f"Core returned {resp.status_code} for location push: {resp.text[:200]}")
            return

        except Exception as exc:
            _log("error", f"HTTP error on location push (attempt {attempt}): {exc}")
            if attempt == 1:
                await asyncio.sleep(5)
