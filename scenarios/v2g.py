"""
V2G scenarios: peak_shaving, solar_storage, frequency_regulation.
"""

import asyncio
import logging

from .base import BaseScenario, ScenarioResult, _get_count

log = logging.getLogger(__name__)


class V2GPeakShaving(BaseScenario):
    name = "v2g_peak_shaving"
    description = "All chargers discharge simultaneously for peak shaving"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        kw = self.params.get("kw", 50)
        duration = self.params.get("duration", 60)
        self._log(f"V2G peak shaving: {count} chargers, {kw}kW each for {duration}s")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"V2G-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(15)
        self._log("Starting V2G discharge...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_v2g(max_discharge_kw=kw)
        await asyncio.sleep(duration)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.stop_v2g()
        return self._finish()


class V2GSolarStorage(BaseScenario):
    name = "v2g_solar_storage"
    description = "Charge during day (solar), discharge in evening"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"V2G solar storage: {count} chargers")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"SOLAR-{i+1:04d}")
        await asyncio.sleep(5)
        self._log("Day phase: charging from solar...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(30)
        self._log("Evening phase: V2G discharge...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_v2g(max_discharge_kw=30)
        await asyncio.sleep(30)
        return self._finish()


class V2GFrequencyRegulation(BaseScenario):
    name = "v2g_frequency_regulation"
    description = "Rapid charge/discharge cycles for frequency regulation"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        cycles = self.params.get("cycles", 10)
        self._log(f"V2G freq regulation: {count} chargers, {cycles} cycles")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"FREQ-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)
        for cycle in range(cycles):
            if self._cancelled:
                break
            self._log(f"Cycle {cycle+1}/{cycles}: discharge")
            for cp_id in self._spawned_ids:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.start_v2g(max_discharge_kw=self._profile_max_kw(cp_id))
            await asyncio.sleep(5)
            self._log(f"Cycle {cycle+1}/{cycles}: charge")
            for cp_id in self._spawned_ids:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.stop_v2g()
            await asyncio.sleep(5)
        return self._finish()

    def _profile_max_kw(self, cp_id: str) -> float:
        charger = self.farm.get_charger(cp_id)
        if charger:
            return charger.profile.max_kw * 0.5
        return 50.0
