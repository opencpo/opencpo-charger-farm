"""
Environmental and fault simulation layer.
Simulates real-world physical conditions, hardware faults,
vehicle-side events, and vandalism/tampering.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Any

log = logging.getLogger(__name__)


class FaultType(str, Enum):
    # Hardware faults
    CONNECTOR_LOCK_FAILURE = "ConnectorLockFailure"
    GROUND_FAILURE = "GroundFailure"
    HIGH_TEMPERATURE = "HighTemperature"
    OVER_CURRENT = "OverCurrentFailure"
    POWER_METER_FAILURE = "PowerMeterFailure"
    UNDER_VOLTAGE = "UnderVoltage"
    OVER_VOLTAGE = "OverVoltage"
    INTERNAL_ERROR = "InternalError"
    # Cooling
    FAN_FAILURE = "FanFailure"
    CONTACTOR_STUCK = "ContactorStuck"

    # Vehicle-side
    BMS_CUTOFF = "BMSCutoff"
    VEHICLE_COMM_LOSS = "VehicleCommLoss"
    CABLE_PULL = "CablePull"
    VEHICLE_POWER_REQUEST = "VehiclePowerRequest"

    # Power grid
    POWER_OUTAGE = "PowerOutage"
    BROWNOUT = "Brownout"
    VOLTAGE_SPIKE = "VoltageSpike"
    ROLLING_BLACKOUT = "RollingBlackout"

    # Environmental
    EMERGENCY_STOP = "EmergencyStop"
    CABLE_CUT = "CableCut"
    UNAUTHORIZED_ACCESS = "UnauthorizedAccess"


@dataclass
class ActiveFault:
    """A currently active fault on a charger or connector."""
    fault_type: FaultType
    connector_id: int = 0             # 0 = charger-level
    start_time: float = 0.0
    duration_sec: Optional[float] = None  # None = permanent until cleared
    severity: str = "error"           # error, warning, info
    stops_session: bool = False
    derating_factor: float = 1.0      # Power multiplier (e.g., 0.5 for brownout)
    message: str = ""

    def is_expired(self) -> bool:
        if self.duration_sec is None:
            return False
        return (time.monotonic() - self.start_time) >= self.duration_sec

    def as_dict(self) -> dict:
        return {
            "fault_type": self.fault_type.value,
            "connector_id": self.connector_id,
            "start_time": self.start_time,
            "duration_sec": self.duration_sec,
            "severity": self.severity,
            "stops_session": self.stops_session,
            "derating_factor": self.derating_factor,
            "message": self.message,
            "expired": self.is_expired(),
        }


@dataclass
class SiteConditions:
    """Shared conditions for all chargers at a site."""
    ambient_temp_c: float = 20.0
    grid_voltage_pct: float = 100.0     # % of nominal (100 = normal)
    grid_frequency_hz: float = 50.0     # Hz (50 = normal for EU)
    power_available: bool = True
    wind_speed_kmh: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ambient_temp_c": self.ambient_temp_c,
            "grid_voltage_pct": self.grid_voltage_pct,
            "grid_frequency_hz": self.grid_frequency_hz,
            "power_available": self.power_available,
            "wind_speed_kmh": self.wind_speed_kmh,
        }


@dataclass
class ChaosConfig:
    """Configuration for random event generation."""
    enabled: bool = False
    intensity: str = "calm"        # calm, stormy, apocalypse

    # Per-hour probability for each event category (0-100)
    hardware_fault_pct: float = 0.0
    vehicle_event_pct: float = 0.0
    power_event_pct: float = 0.0
    network_event_pct: float = 0.0

    def set_intensity(self, level: str) -> None:
        self.intensity = level
        if level == "calm":
            self.hardware_fault_pct = 2.0
            self.vehicle_event_pct = 5.0
            self.power_event_pct = 1.0
            self.network_event_pct = 3.0
        elif level == "stormy":
            self.hardware_fault_pct = 10.0
            self.vehicle_event_pct = 15.0
            self.power_event_pct = 5.0
            self.network_event_pct = 10.0
        elif level == "apocalypse":
            self.hardware_fault_pct = 25.0
            self.vehicle_event_pct = 30.0
            self.power_event_pct = 15.0
            self.network_event_pct = 25.0

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "intensity": self.intensity,
            "hardware_fault_pct": self.hardware_fault_pct,
            "vehicle_event_pct": self.vehicle_event_pct,
            "power_event_pct": self.power_event_pct,
            "network_event_pct": self.network_event_pct,
        }


class EnvironmentSimulator:
    """
    Manages environmental conditions, hardware faults, and chaos events
    for a set of chargers at a site.
    """

    def __init__(self):
        self.site = SiteConditions()
        self.chaos = ChaosConfig()
        self._active_faults: dict[str, list[ActiveFault]] = {}  # cp_id → faults
        self._chaos_task: Optional[asyncio.Task] = None

    def get_faults(self, cp_id: str) -> list[ActiveFault]:
        """Get active (non-expired) faults for a charger."""
        if cp_id not in self._active_faults:
            return []
        # Prune expired faults
        self._active_faults[cp_id] = [
            f for f in self._active_faults[cp_id] if not f.is_expired()
        ]
        return self._active_faults[cp_id]

    def add_fault(self, cp_id: str, fault: ActiveFault) -> None:
        if cp_id not in self._active_faults:
            self._active_faults[cp_id] = []
        fault.start_time = time.monotonic()
        self._active_faults[cp_id].append(fault)
        log.info("[%s] Fault injected: %s (connector=%d)", cp_id, fault.fault_type.value, fault.connector_id)

    def clear_fault(self, cp_id: str, fault_type: FaultType, connector_id: int = 0) -> bool:
        """Clear a specific fault. Returns True if found and removed."""
        if cp_id not in self._active_faults:
            return False
        before = len(self._active_faults[cp_id])
        self._active_faults[cp_id] = [
            f for f in self._active_faults[cp_id]
            if not (f.fault_type == fault_type and f.connector_id == connector_id)
        ]
        return len(self._active_faults[cp_id]) < before

    def clear_all_faults(self, cp_id: str) -> None:
        self._active_faults.pop(cp_id, None)

    def get_derating_factor(self, cp_id: str) -> float:
        """Combined derating factor from all active faults."""
        faults = self.get_faults(cp_id)
        factor = 1.0
        for f in faults:
            factor *= f.derating_factor
        return factor

    def has_session_stopping_fault(self, cp_id: str, connector_id: int = 0) -> Optional[ActiveFault]:
        """Return a session-stopping fault if any is active."""
        for f in self.get_faults(cp_id):
            if f.stops_session and (f.connector_id == 0 or f.connector_id == connector_id):
                return f
        return None

    def status(self) -> dict:
        return {
            "site": self.site.as_dict(),
            "chaos": self.chaos.as_dict(),
            "faults_by_charger": {
                cp_id: [f.as_dict() for f in faults]
                for cp_id, faults in self._active_faults.items()
                if faults
            },
        }

    # ─── Fault factory methods ───────────────────────────────────────────

    @staticmethod
    def make_connector_lock_failure(connector_id: int = 1) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.CONNECTOR_LOCK_FAILURE,
            connector_id=connector_id,
            stops_session=True,
            message="Connector lock mechanism failure",
        )

    @staticmethod
    def make_ground_failure(connector_id: int = 1) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.GROUND_FAILURE,
            connector_id=connector_id,
            stops_session=True,
            message="Ground fault detected — immediate session stop",
        )

    @staticmethod
    def make_high_temperature(connector_id: int = 0, duration_sec: float = 120) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.HIGH_TEMPERATURE,
            connector_id=connector_id,
            duration_sec=duration_sec,
            derating_factor=0.5,
            message="High temperature — power derated to 50%",
        )

    @staticmethod
    def make_over_current(connector_id: int = 1) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.OVER_CURRENT,
            connector_id=connector_id,
            stops_session=True,
            message="Over-current protection triggered",
        )

    @staticmethod
    def make_power_meter_failure() -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.POWER_METER_FAILURE,
            severity="warning",
            message="Power meter communication failure — MeterValues unavailable",
        )

    @staticmethod
    def make_under_voltage(duration_sec: float = 60) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.UNDER_VOLTAGE,
            duration_sec=duration_sec,
            derating_factor=0.6,
            message="Grid undervoltage — power reduced",
        )

    @staticmethod
    def make_fan_failure(duration_sec: Optional[float] = None) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.FAN_FAILURE,
            duration_sec=duration_sec,
            derating_factor=0.5,
            severity="warning",
            message="Cooling fan failure — gradual power derating",
        )

    @staticmethod
    def make_power_outage() -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.POWER_OUTAGE,
            stops_session=True,
            derating_factor=0.0,
            message="Power outage — charger offline",
        )

    @staticmethod
    def make_emergency_stop() -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.EMERGENCY_STOP,
            stops_session=True,
            message="Emergency stop pressed — charger unavailable",
        )

    @staticmethod
    def make_cable_pull(connector_id: int = 1) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.CABLE_PULL,
            connector_id=connector_id,
            stops_session=True,
            message="Cable pulled during charging — EVDisconnected",
        )

    @staticmethod
    def make_bms_cutoff(connector_id: int = 1, soc: float = 80.0) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.BMS_CUTOFF,
            connector_id=connector_id,
            stops_session=True,
            message=f"Vehicle BMS hard cutoff at {soc}% SoC",
        )

    @staticmethod
    def make_brownout(duration_sec: float = 300) -> ActiveFault:
        return ActiveFault(
            fault_type=FaultType.BROWNOUT,
            duration_sec=duration_sec,
            derating_factor=0.7,
            message="Grid brownout — power reduced to 70%",
        )
