"""
Chaos and weather scenarios.
"""

import asyncio
import logging
import random
import time

from .base import BaseScenario, ScenarioResult, _get_count

log = logging.getLogger(__name__)


class Chaos(BaseScenario):
    name = "chaos"
    description = "Random real-world events at varying intensity"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        duration = self.params.get("duration", 120)
        chaos_intensity = self.params.get("chaos_intensity", self.intensity)
        self._log(f"Chaos: {count} chargers, {duration}s, intensity={chaos_intensity}")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"CHAOS-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()

        end_time = time.monotonic() + duration
        while time.monotonic() < end_time and not self._cancelled:
            cp_id = random.choice(self._spawned_ids)
            charger = self.farm.get_charger(cp_id)
            if not charger:
                continue
            action = random.choice(["disconnect", "error", "start", "stop", "v2g_start", "v2g_stop"])
            try:
                if action == "disconnect":
                    await charger.force_disconnect()
                    self._log(f"Chaos: disconnected {cp_id}")
                elif action == "error":
                    errors = ["GroundFailure", "OverCurrentFailure", "HighTemperature", "InternalError"]
                    await charger.inject_error(random.choice(errors))
                elif action == "start":
                    await charger.start_charging()
                elif action == "stop":
                    await charger.stop_charging()
                elif action == "v2g_start":
                    await charger.start_v2g()
                elif action == "v2g_stop":
                    await charger.stop_v2g()
            except Exception as e:
                self._log(f"Chaos action {action} failed: {e}")
            wait = {"low": 5, "medium": 2, "high": 0.5, "extreme": 0.1}.get(chaos_intensity, 2)
            await asyncio.sleep(random.uniform(wait * 0.5, wait * 1.5))
        return self._finish()


class WinterStress(BaseScenario):
    name = "winter_stress"
    description = "Cold weather charging with vehicle preconditioning"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Winter stress: {count} chargers at -10°C")
        self.farm.env_sim.site.ambient_temp_c = -10.0
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"WINTER-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(self.params.get("hold_sec", 60))
        self.farm.env_sim.site.ambient_temp_c = 20.0
        return self._finish()


class SummerPeak(BaseScenario):
    name = "summer_peak"
    description = "Hot weather derating with high demand"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Summer peak: {count} chargers at 42°C")
        self.farm.env_sim.site.ambient_temp_c = 42.0
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"SUMMER-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(self.params.get("hold_sec", 60))
        self.farm.env_sim.site.ambient_temp_c = 20.0
        return self._finish()


class NetworkHell(BaseScenario):
    name = "network_hell"
    description = "Every network problem simultaneously"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Network hell: {count} chargers")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"NETHELL-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                charger.network.config.enabled = True
                charger.network.config.latency_ms = random.uniform(500, 5000)
                charger.network.config.jitter_ms = random.uniform(100, 1000)
                charger.network.config.packet_loss_pct = random.uniform(10, 50)
                await charger.start_charging()
        await asyncio.sleep(self.params.get("hold_sec", 60))
        return self._finish()


class SitePowerEvent(BaseScenario):
    name = "site_power_event"
    description = "Cascading power outage across chargers"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Site power event: {count} chargers")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"POWER-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)
        self._log("Power outage starting...")
        for cp_id in self._spawned_ids:
            if self._cancelled:
                break
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.force_disconnect()
                self._log(f"Power lost: {cp_id}")
            await asyncio.sleep(random.uniform(0.1, 0.5))
        self._log("Waiting for power restoration...")
        await asyncio.sleep(15)
        self._log("Power restored — chargers reconnecting...")
        await asyncio.sleep(self.params.get("hold_sec", 45))
        return self._finish()
