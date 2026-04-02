"""
Base scenario class and shared utilities.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from metrics import farm_metrics
from profiles import QuirkConfig

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
            "events": self.events[-100:],
        }


class BaseScenario:
    """Base class for all scenarios."""

    name: str = "base"
    description: str = ""

    def __init__(self, farm, intensity: str = "medium", **kwargs):
        self.farm = farm
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
                              quirks: Optional[QuirkConfig] = None,
                              ocpp_version: str = "1.6") -> Optional[str]:
        if self._cancelled:
            return None
        charger = await self.farm.spawn_charger(cp_id, profile_name, quirks=quirks)
        if charger:
            self._spawned_ids.append(cp_id)
        return cp_id if charger else None

    async def _cleanup_spawned(self):
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
