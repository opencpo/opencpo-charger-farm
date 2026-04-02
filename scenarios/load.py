"""
Load scenarios: ramp_up, peak_load, reconnect_flood.
"""

import asyncio
import logging
import random

from .base import BaseScenario, ScenarioResult, _get_count
from profiles import MAXPOWER_QUIRKS

log = logging.getLogger(__name__)


class RampUp(BaseScenario):
    name = "ramp_up"
    description = "Spawn chargers one by one at a steady interval"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("target"))
        interval = self.params.get("interval_sec", 2.0)
        self._log(f"Ramping up {count} chargers, interval={interval}s")
        for i in range(count):
            if self._cancelled:
                break
            cp_id = f"RAMP-{i+1:04d}"
            await self._spawn_charger(cp_id)
            await asyncio.sleep(interval)
            charger = self.farm.get_charger(cp_id)
            if charger:
                await asyncio.sleep(1)
                await charger.start_charging()
        self._log(f"Ramp complete: {len(self._spawned_ids)} chargers")
        await asyncio.sleep(self.params.get("hold_sec", 30))
        return self._finish()


class PeakLoad(BaseScenario):
    name = "peak_load"
    description = "All chargers connect and charge simultaneously"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Peak load: spawning {count} chargers simultaneously")
        tasks = []
        for i in range(count):
            if self._cancelled:
                break
            tasks.append(self._spawn_charger(f"PEAK-{i+1:04d}"))
        await asyncio.gather(*tasks, return_exceptions=True)
        self._log(f"All {len(self._spawned_ids)} chargers spawned, starting sessions...")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(self.params.get("hold_sec", 60))
        return self._finish()


class DisconnectStorm(BaseScenario):
    name = "disconnect_storm"
    description = "Random WebSocket disconnects on active chargers"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        pct = self.params.get("pct", 50)
        delay = self.params.get("delay", 1.0)
        self._log(f"Disconnect storm: {count} chargers, {pct}% disconnect")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"STORM-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)
        to_disconnect = random.sample(self._spawned_ids, k=min(int(count * pct / 100), len(self._spawned_ids)))
        for cp_id in to_disconnect:
            if self._cancelled:
                break
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.force_disconnect()
                self._log(f"Disconnected {cp_id}")
                await asyncio.sleep(delay)
        await asyncio.sleep(self.params.get("hold_sec", 30))
        return self._finish()


class ReconnectFlood(BaseScenario):
    name = "reconnect_flood"
    description = "All chargers disconnect then reconnect at once"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Reconnect flood: {count} chargers")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"FLOOD-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)
        self._log("Disconnecting all...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.force_disconnect()
        await asyncio.sleep(2)
        self._log("All disconnected, waiting for reconnect flood...")
        await asyncio.sleep(self.params.get("hold_sec", 60))
        return self._finish()
