"""
Stress test scenarios for the virtual charger farm.
"""

from .base import BaseScenario, ScenarioResult, _get_count, INTENSITY_COUNTS
from .load import RampUp, PeakLoad, DisconnectStorm, ReconnectFlood
from .sessions import MixedSessions, FirmwareQuirks, SessionPersistence, Endurance, PnCFlow
from .chaos import Chaos, WinterStress, SummerPeak, NetworkHell, SitePowerEvent
from .v2g import V2GPeakShaving, V2GSolarStorage, V2GFrequencyRegulation

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


__all__ = [
    "BaseScenario", "ScenarioResult", "SCENARIOS", "list_scenarios",
    "INTENSITY_COUNTS", "_get_count",
]
