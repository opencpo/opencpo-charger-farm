"""
Stress test scenarios for the virtual charger farm.
Each scenario is a class with async run() method.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Any

from profiles import PROFILES, get_profile, ChargerProfile, QuirkConfig, MAXPOWER_QUIRKS, NO_QUIRKS, OcppVersion
from physics import V2GConfig, PowerDirection
from metrics import farm_metrics
from environment import EnvironmentSimulator, FaultType, ChaosConfig
from network import NetworkConfig

log = logging.getLogger(__name__)

# Intensity levels
INTENSITY_COUNTS = {"low": 5, "medium": 20, "high": 50, "extreme": 100}


def _get_count(intensity: str, override: Optional[int] = None) -> int:
    if override is not None:
        return override
    return INTENSITY_COUNTS.get(intensity, 20)


@dataclass
class ScenarioResult:
    name: str
    started_at: float
    ended_at: float = 0.0
    charger_count: int = 0
    intensity: str = "medium"
    success: bool = True
    error: Optional[str] = None
    metrics_snapshot: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    @property
    def duration_sec(self) -> float:
        return self.ended_at - self.started_at if self.ended_at else time.time() - self.started_at

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_sec": round(self.duration_sec, 1),
            "charger_count": self.charger_count,
            "intensity": self.intensity,
            "success": self.success,
            "error": self.error,
            "metrics_snapshot": self.metrics_snapshot,
            "events": self.events[-100],  # Last 100 events
        }


class BaseScenario:
    """Base class for all scenarios."""

    name: str = "base"
    description: str = ""

    def __init__(self, farm, intensity: str = "medium", **kwargs):
        """
        Args:
            farm: reference to the control plane's charger farm (dict of cp_id -> charger)
            intensity: low/medium/high/extreme
        """
        self.farm = farm  # Reference to control.py's charger registry
        self.intensity = intensity
        self.params = kwargs
        self._cancelled = False
        self._result = ScenarioResult(name=self.name, started_at=time.time(), intensity=intensity)
        self._spawned_ids: list[str] = []

    @property
    def progress(self) -> float:
        return 0.0

    def cancel(self):
        self._cancelled = True

    async def run(self) -> ScenarioResult:
        raise NotImplementedError

    def _log(self, msg: str):
        farm_metrics.log_event("info", f"scenario:{self.name}", msg)
        self._result.events.append({"time": time.time(), "msg": msg})

    async def _spawn_charger(self, cp_id: str, profile_name: str = "ENC-DCL120B-16",
                              quirks: Optional[QuirkConfig] = None, ocpp_version: str = "1.6") -> Optional[str]:
        """Spawn a charger via the farm's spawn function."""
        if self._cancelled:
            return None
        # The farm object should have a spawn_charger method
        charger = await self.farm.spawn_charger(cp_id, profile_name, quirks=quirks)
        if charger:
            self._spawned_ids.append(cp_id)
        return cp_id if charger else None

    async def _cleanup_spawned(self):
        """Stop all chargers spawned by this scenario."""
        for cp_id in self._spawned_ids:
            try:
                await self.farm.stop_charger(cp_id)
            except Exception:
                pass

    def _finish(self, success: bool = True, error: str = None):
        self._result.ended_at = time.time()
        self._result.success = success
        self._result.error = error
        self._result.metrics_snapshot = farm_metrics.snapshot()
        self._result.charger_count = len(self._spawned_ids)
        return self._result


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
            # Start charging on each
            charger = self.farm.get_charger(cp_id)
            if charger:
                await asyncio.sleep(1)
                await charger.start_charging()

        self._log(f"Ramp complete: {len(self._spawned_ids)} chargers")
        # Let it run for a bit
        await asyncio.sleep(self.params.get("hold_sec", 30))
        return self._finish()


class PeakLoad(BaseScenario):
    name = "peak_load"
    description = "All chargers connect and charge simultaneously"

    async def run(self) -> ScenarioResult:
        count = _get_count(self.intensity, self.params.get("count"))
        self._log(f"Peak load: spawning {count} chargers simultaneously")

        # Spawn all at once
        tasks = []
        for i in range(count):
            if self._cancelled:
                break
            cp_id = f"PEAK-{i+1:04d}"
            tasks.append(self._spawn_charger(cp_id))

        await asyncio.gather(*tasks, return_exceptions=True)
        self._log(f"All {len(self._spawned_ids)} chargers spawned, starting sessions...")

        # Start all charging
        await asyncio.sleep(5)  # Wait for connections
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()

        # Hold for observation
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

        # Spawn and start
        for i in range(count):
            if self._cancelled:
                break
            cp_id = f"STORM-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()

        await asyncio.sleep(10)  # Let sessions establish

        # Random disconnects
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
            cp_id = f"FLOOD-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        # Start sessions
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)

        # Disconnect all
        self._log("Disconnecting all...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.force_disconnect()

        await asyncio.sleep(2)

        # Reconnect all simultaneously (they auto-reconnect)
        self._log("All disconnected, waiting for reconnect flood...")
        await asyncio.sleep(self.params.get("hold_sec", 60))
        return self._finish()


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
            cp_id = f"MIX-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        end_time = time.monotonic() + duration
        while time.monotonic() < end_time and not self._cancelled:
            cp_id = random.choice(self._spawned_ids)
            charger = self.farm.get_charger(cp_id)
            if charger:
                # Randomly start or stop
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
            cp_id = f"QUIRK-{i+1:04d}"
            await self._spawn_charger(cp_id, quirks=MAXPOWER_QUIRKS)
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
            cp_id = f"PERSIST-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        # Start all
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)

        if pattern == "clean":
            # Clean disconnect: StopTransaction then disconnect
            for cp_id in self._spawned_ids[:len(self._spawned_ids)//2]:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.stop_charging(reason="Local")
                    await asyncio.sleep(1)
                    await charger.force_disconnect()
        elif pattern == "dirty":
            # Dirty: disconnect without StopTransaction
            for cp_id in self._spawned_ids[:len(self._spawned_ids)//2]:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.force_disconnect()
        elif pattern == "flap":
            # Rapid connect/disconnect
            for _ in range(5):
                for cp_id in self._spawned_ids:
                    charger = self.farm.get_charger(cp_id)
                    if charger:
                        await charger.force_disconnect()
                await asyncio.sleep(3)
                # Auto-reconnect handles it
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
            cp_id = f"V2G-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        # Start charging first (need active session for V2G)
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(15)

        # Switch to discharge
        self._log("Starting V2G discharge...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_v2g(max_discharge_kw=kw)

        await asyncio.sleep(duration)

        # Stop V2G
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
            cp_id = f"SOLAR-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        # Phase 1: Charge (simulate day)
        self._log("Day phase: charging from solar...")
        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(30)

        # Phase 2: Discharge (simulate evening)
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
            cp_id = f"FREQ-{i+1:04d}"
            await self._spawn_charger(cp_id)
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
                    await charger.start_v2g(max_discharge_kw=self.profile_max_kw(cp_id))
            await asyncio.sleep(5)

            self._log(f"Cycle {cycle+1}/{cycles}: charge")
            for cp_id in self._spawned_ids:
                charger = self.farm.get_charger(cp_id)
                if charger:
                    await charger.stop_v2g()
            await asyncio.sleep(5)

        return self._finish()

    def profile_max_kw(self, cp_id: str) -> float:
        charger = self.farm.get_charger(cp_id)
        if charger:
            return charger.profile.max_kw * 0.5
        return 50.0


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
            cp_id = f"CHAOS-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        # Start sessions
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

            action = random.choice([
                "disconnect", "error", "start", "stop", "v2g_start", "v2g_stop",
            ])

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

        # Set cold environment
        self.farm.env_sim.site.ambient_temp_c = -10.0

        for i in range(count):
            if self._cancelled:
                break
            cp_id = f"WINTER-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()

        await asyncio.sleep(self.params.get("hold_sec", 60))
        self.farm.env_sim.site.ambient_temp_c = 20.0  # Reset
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
            cp_id = f"SUMMER-{i+1:04d}"
            await self._spawn_charger(cp_id)
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
            cp_id = f"NETHELL-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        # Configure network degradation
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
            cp_id = f"POWER-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()
        await asyncio.sleep(10)

        # Cascading outage
        self._log("Power outage starting...")
        for i, cp_id in enumerate(self._spawned_ids):
            if self._cancelled:
                break
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.force_disconnect()
                self._log(f"Power lost: {cp_id}")
            await asyncio.sleep(random.uniform(0.1, 0.5))

        # Power restored after delay
        self._log("Waiting for power restoration...")
        await asyncio.sleep(15)
        self._log("Power restored — chargers reconnecting...")
        await asyncio.sleep(self.params.get("hold_sec", 45))
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
            cp_id = f"ENDURE-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                await charger.start_charging()

        end_time = time.monotonic() + duration
        event_interval = 30  # Random event every 30s

        while time.monotonic() < end_time and not self._cancelled:
            await asyncio.sleep(event_interval)
            # Random event
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

        from pnc import PnCConfig

        for i in range(count):
            if self._cancelled:
                break
            cp_id = f"PNC-{i+1:04d}"
            await self._spawn_charger(cp_id)
        await asyncio.sleep(5)

        for cp_id in self._spawned_ids:
            charger = self.farm.get_charger(cp_id)
            if charger:
                charger.pnc_config.enabled = True
                await charger.trigger_pnc()
                await asyncio.sleep(1)

        await asyncio.sleep(self.params.get("hold_sec", 30))
        return self._finish()


# ─── Scenario Registry ───────────────────────────────────────────────────────

SCENARIOS: dict[str, type[BaseScenario]] = {
    "ramp_up": RampUp,
    "peak_load": PeakLoad,
    "disconnect_storm": DisconnectStorm,
    "reconnect_flood": ReconnectFlood,
    "mixed_sessions": MixedSessions,
    "firmware_quirks": FirmwareQuirks,
    "session_persistence": SessionPersistence,
    "v2g_peak_shaving": V2GPeakShaving,
    "v2g_solar_storage": V2GSolarStorage,
    "v2g_frequency_regulation": V2GFrequencyRegulation,
    "chaos": Chaos,
    "winter_stress": WinterStress,
    "summer_peak": SummerPeak,
    "network_hell": NetworkHell,
    "site_power_event": SitePowerEvent,
    "endurance": Endurance,
    "pnc_flow": PnCFlow,
}


def list_scenarios() -> list[dict]:
    return [
        {"name": name, "description": cls.description}
        for name, cls in SCENARIOS.items()
    ]
