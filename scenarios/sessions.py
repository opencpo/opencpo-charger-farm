"""
Session scenarios: mixed_sessions, session_persistence, firmware_quirks.
"""

import asyncio
import logging
import random
import time

from .base import BaseScenario, ScenarioResult, _get_count
from profiles import MAXPOWER_QUIRKS

log = logging.getLogger(__name__)


class MixedSessions(BaseScenario):
    name = "mixed_sessions"
    description = "Staggered start/stop of charging sessions"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        duration = self.params.get("duration", 120)
        self._log(f"Mixed sessions: {count} chargers over {duration}s")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"MIX-{i+1:04d}")
        await asyncio.sleep(5)
        end_time = time.monotonic() + duration
        while time.monotonic() < end_time and not self._cancelled:
            cp_id = random.choice(self._spawned_ids)
            charger = self.farm.get_charger(cp_id)
            if charger:
                if random.random() < 0.6:
                    await charger.start_charging()
                else:
                    await charger.stop_charging()
            await asyncio.sleep(random.uniform(1, 5))
        return self._finish()


class FirmwareQuirks(BaseScenario):
    name = "firmware_quirks"
    description = "All chargers with all MAXPOWER quirks enabled"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Firmware quirks: {count} chargers with all quirks")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"QUIRK-{i+1:04d}", quirks=MAXPOWER_QUIRKS)
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(self.params.get("hold_sec", 60))
        return self._finish()


class SessionPersistence(BaseScenario):
    name = "session_persistence"
    description = "Test session persistence across disconnect patterns"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        pattern = self.params.get("pattern", "flap")
        self._log(f"Session persistence: {count} chargers, pattern={pattern}")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"PERSIST-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)

        half = len(self._spawned_ids) // 2
        if pattern == "clean":
            for cp_id in self._spawned_ids[:half]:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.stop_charging(reason="Local")
                    await asyncio.sleep(1)
                    await charger.force_disconnect()
        elif pattern == "dirty":
            for cp_id in self._spawned_ids[:half]:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.force_disconnect()
        elif pattern == "flap":
            for _ in range(5):
                for cp_id in self._spawned_ids:
                    charger = self.farm.get_charger(cp_id)
                    if charger:
                        await charger.force_disconnect()
                await asyncio.sleep(3)
                await asyncio.sleep(10)
        elif pattern == "long_outage":
            for cp_id in self._spawned_ids:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.force_disconnect()
            self._log("Long outage: waiting 60s...")
            await asyncio.sleep(60)

        await asyncio.sleep(self.params.get("hold_sec", 30))
        return self._finish()


class Endurance(BaseScenario):
    name = "endurance"
    description = "Long-running test with periodic random events"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        hours = self.params.get("hours", 1)
        duration = hours * 3600
        self._log(f"Endurance: {count} chargers for {hours}h")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"ENDURE-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        end_time = time.monotonic() + duration
        while time.monotonic() < end_time and not self._cancelled:
            await asyncio.sleep(30)
            cp_id = random.choice(self._spawned_ids)
            charger = self.farm.get_charger(cp_id)
            if charger:
                action = random.choice(["restart_session", "disconnect", "nothing", "nothing"])
                if action == "restart_session":
                    await charger.stop_charging()
                    await asyncio.sleep(2)
                    await charger.start_charging()
                elif action == "disconnect":
                    await charger.force_disconnect()
        return self._finish()


class PnCFlow(BaseScenario):
    name = "pnc_flow"
    description = "Plug & Charge certification test"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"PnC flow: {count} chargers")
        for i in range(count):
            if self._cancelled:
                break
            await self._spawn_charger(f"PNC-{i+1:04d}")
        await asyncio.sleep(5)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                charger.pnc_config.enabled = True
                await charger.trigger_pnc()
                await asyncio.sleep(1)
        await asyncio.sleep(self.params.get("hold_sec", 30))
        return self._finish()
