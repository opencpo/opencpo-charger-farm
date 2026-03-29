"""
Shared DC fast charge / V2G discharge physics.
Realistic simulation of ramp → bulk → taper → trickle charge phases,
bidirectional power flow, environmental derating, and vehicle behavior.
"""

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


NOMINAL_VOLTAGE = 400.0
DEFAULT_BATTERY_KWH = 75.0


class PowerDirection(str, Enum):
    IDLE = "idle"
    CHARGING = "charging"
    DISCHARGING = "discharging"


@dataclass
class EnvironmentConditions:
    """Ambient conditions affecting charge behavior."""
    ambient_temp_c: float = 20.0           # °C — affects derating
    vehicle_precondition: bool = False      # Cold battery needs warmup
    bms_cutoff_soc: Optional[float] = None # Vehicle BMS hard cutoff SoC (None = normal)

    def power_derating_factor(self) -> float:
        """Temperature-based power derating. Returns 0.0-1.0 multiplier."""
        t = self.ambient_temp_c
        if t <= -10:
            return 0.4
        elif t < 0:
            return 0.4 + 0.3 * ((t + 10) / 10.0)
        elif t < 10:
            return 0.7 + 0.2 * (t / 10.0)
        elif t <= 35:
            return 1.0
        elif t < 45:
            return 1.0 - 0.4 * ((t - 35) / 10.0)
        else:
            return 0.5

    def ramp_duration_sec(self) -> float:
        """Cold weather = longer ramp phase."""
        if self.ambient_temp_c < 0:
            return 90.0  # 90s ramp in freezing
        elif self.ambient_temp_c < 10:
            return 60.0
        return 30.0


@dataclass
class V2GConfig:
    """Vehicle-to-Grid configuration."""
    enabled: bool = False
    max_discharge_kw: float = 50.0
    min_soc_floor: float = 20.0          # Never discharge below this
    ramp_sec: float = 10.0               # Transition ramp time


@dataclass
class ChargingProfileLimit:
    """Active power limit from a smart charging profile."""
    limit_kw: float
    stack_level: int = 0
    profile_purpose: str = "TxDefaultProfile"
    start_time: Optional[float] = None
    duration_sec: Optional[float] = None

    def is_active(self, elapsed: float) -> bool:
        if self.start_time is not None and elapsed < self.start_time:
            return False
        if self.start_time is not None and self.duration_sec is not None:
            if elapsed > self.start_time + self.duration_sec:
                return False
        return True


@dataclass
class ChargeState:
    """Mutable state for one charging session's physics."""
    soc: float
    meter_wh: float                       # Cumulative charge energy (Wh)
    max_kw: float
    battery_kwh: float = DEFAULT_BATTERY_KWH
    start_soc: float = 0.0
    elapsed_sec: float = 0.0
    current_power_kw: float = 0.0
    current_voltage: float = NOMINAL_VOLTAGE
    current_current_a: float = 0.0

    # V2G
    direction: PowerDirection = PowerDirection.CHARGING
    discharge_meter_wh: float = 0.0       # Cumulative discharge energy (Wh)
    v2g: V2GConfig = field(default_factory=V2GConfig)

    # Environment
    env: EnvironmentConditions = field(default_factory=EnvironmentConditions)

    # Smart charging limits (highest stack_level wins)
    charging_profiles: list[ChargingProfileLimit] = field(default_factory=list)

    # Hardware derating (e.g., cooling fan failure)
    hardware_derating: float = 1.0        # 0.0-1.0 multiplier

    # Forced stop flags
    bms_cutoff: bool = False
    cable_disconnected: bool = False

    def __post_init__(self):
        self.start_soc = self.soc

    def get_effective_limit_kw(self) -> float:
        """Get the effective power limit considering all active charging profiles."""
        active = [p for p in self.charging_profiles if p.is_active(self.elapsed_sec)]
        if not active:
            return self.max_kw
        # Highest stack level wins; if negative, it's a discharge command
        best = max(active, key=lambda p: p.stack_level)
        return best.limit_kw

    def compute_charge_power_kw(self) -> float:
        """Compute charging power (positive = charge into vehicle)."""
        ramp_dur = self.env.ramp_duration_sec()

        if self.elapsed_sec < ramp_dur:
            base = self.max_kw * (self.elapsed_sec / ramp_dur)
        elif self.soc < 80:
            base = self.max_kw
        elif self.soc < 95:
            fraction = (self.soc - 80) / 15.0
            base = self.max_kw * (1.0 - 0.7 * fraction)
        else:
            base = self.max_kw * 0.1

        # Apply environmental derating
        base *= self.env.power_derating_factor()

        # Apply hardware derating
        base *= self.hardware_derating

        # Apply smart charging profile limit
        profile_limit = self.get_effective_limit_kw()
        if profile_limit >= 0:
            base = min(base, profile_limit)

        # BMS cutoff check
        if self.env.bms_cutoff_soc is not None and self.soc >= self.env.bms_cutoff_soc:
            self.bms_cutoff = True
            return 0.0

        # ±2% jitter
        jitter = random.uniform(-0.02, 0.02)
        return max(0.0, base * (1 + jitter))

    def compute_discharge_power_kw(self) -> float:
        """Compute V2G discharge power (returned as positive kW being exported)."""
        if not self.v2g.enabled:
            return 0.0
        if self.soc <= self.v2g.min_soc_floor:
            return 0.0

        # Check if a charging profile commands discharge (negative limit)
        profile_limit = self.get_effective_limit_kw()
        if profile_limit >= 0:
            discharge_kw = self.v2g.max_discharge_kw
        else:
            discharge_kw = min(abs(profile_limit), self.v2g.max_discharge_kw)

        # Apply derating
        discharge_kw *= self.env.power_derating_factor()
        discharge_kw *= self.hardware_derating

        # Ramp
        # Use a short ramp at transition start (simplified: always apply max for now)
        jitter = random.uniform(-0.02, 0.02)
        return max(0.0, discharge_kw * (1 + jitter))

    def tick(self, dt_sec: float) -> "MeterSnapshot":
        """Advance the simulation by dt_sec seconds."""
        self.elapsed_sec += dt_sec

        if self.cable_disconnected or self.bms_cutoff:
            return self._snapshot(0.0, 0.0)

        if self.direction == PowerDirection.CHARGING:
            power_kw = self.compute_charge_power_kw()
            energy_kwh = power_kw * (dt_sec / 3600.0)
            self.meter_wh += energy_kwh * 1000.0
            self.soc += (energy_kwh / self.battery_kwh) * 100.0
            self.soc = min(100.0, self.soc)
            return self._snapshot(power_kw, 0.0)

        elif self.direction == PowerDirection.DISCHARGING:
            discharge_kw = self.compute_discharge_power_kw()
            energy_kwh = discharge_kw * (dt_sec / 3600.0)
            self.discharge_meter_wh += energy_kwh * 1000.0
            self.soc -= (energy_kwh / self.battery_kwh) * 100.0
            self.soc = max(0.0, self.soc)
            if self.soc <= self.v2g.min_soc_floor:
                self.direction = PowerDirection.IDLE
            return self._snapshot(0.0, discharge_kw)

        else:  # IDLE
            return self._snapshot(0.0, 0.0)

    def _snapshot(self, charge_kw: float, discharge_kw: float) -> "MeterSnapshot":
        net_power_kw = charge_kw - discharge_kw
        net_power_w = net_power_kw * 1000.0
        self.current_voltage = NOMINAL_VOLTAGE * random.uniform(0.95, 1.05)
        self.current_current_a = abs(net_power_w) / self.current_voltage if self.current_voltage > 0 else 0.0
        self.current_power_kw = net_power_kw

        return MeterSnapshot(
            power_w=round(charge_kw * 1000, 1),
            power_export_w=round(discharge_kw * 1000, 1),
            energy_wh=round(self.meter_wh),
            energy_export_wh=round(self.discharge_meter_wh),
            current_a=round(self.current_current_a, 1),
            voltage=round(self.current_voltage, 1),
            soc=round(self.soc, 1),
            power_kw=round(charge_kw, 2),
            discharge_kw=round(discharge_kw, 2),
            net_power_kw=round(net_power_kw, 2),
            elapsed_sec=round(self.elapsed_sec, 1),
            direction=self.direction.value,
        )

    @property
    def is_full(self) -> bool:
        return self.soc >= 100.0


@dataclass(frozen=True)
class MeterSnapshot:
    """Immutable snapshot of all meter values."""
    power_w: float
    power_export_w: float
    energy_wh: float
    energy_export_wh: float
    current_a: float
    voltage: float
    soc: float
    power_kw: float
    discharge_kw: float
    net_power_kw: float
    elapsed_sec: float
    direction: str

    def as_dict(self) -> dict:
        return {
            "power_w": self.power_w,
            "power_export_w": self.power_export_w,
            "energy_wh": self.energy_wh,
            "energy_export_wh": self.energy_export_wh,
            "current_a": self.current_a,
            "voltage": self.voltage,
            "soc": self.soc,
            "power_kw": self.power_kw,
            "discharge_kw": self.discharge_kw,
            "net_power_kw": self.net_power_kw,
            "elapsed_sec": self.elapsed_sec,
            "direction": self.direction,
        }


def random_start_soc(low: float = 10.0, high: float = 50.0) -> float:
    return round(random.uniform(low, high), 1)
