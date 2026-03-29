# Physics Simulation

The virtual charger uses a physics model to generate realistic power and energy readings. This ensures that MeterValues sent to the CSMS reflect plausible EV charging behavior rather than flat numbers.

## Charge Curve

DC fast charging follows a characteristic curve with four phases:

```
Power
  |
  |         ┌───────────────────┐
  │        /                     \
  │       /                       \
  │      /                         \____
  │     /                               \
  │    /                                 \___
  └──────────────────────────────────────────── SoC
     0      Ramp   80%           95%      100%
            Phase  Bulk end      Taper end
```

### Phases

**Ramp (0 → max power over `ramp_duration_sec`):**
```python
power_kw = max_kw * (elapsed_sec / ramp_duration_sec)
```
Default ramp: 30s at 20°C. Cold weather extends this — see [Temperature Derating](#temperature-derating).

**Bulk (SoC 0–80%):**
```python
power_kw = max_kw  # Constant at rated power
```
The bulk phase delivers the majority of the session's energy.

**Taper (SoC 80–95%):**
```python
fraction = (soc - 80) / 15.0
power_kw = max_kw * (1.0 - 0.7 * fraction)
# At 80% SoC: 100% power. At 95% SoC: 30% power.
```

**Trickle (SoC > 95%):**
```python
power_kw = max_kw * 0.1
```

### Jitter

All power readings include ±2% random jitter to simulate real measurement noise:
```python
power_kw *= random.uniform(0.98, 1.02)
```

## State of Charge (SoC) Modeling

The battery is modeled as a simple capacity bucket:

```python
# On each tick (dt seconds)
energy_kwh = power_kw * (dt / 3600.0)
soc += (energy_kwh / battery_kwh) * 100.0
```

Default battery: 75 kWh. Override per scenario with `battery_kwh` in `ChargeState`.

Initial SoC is randomized per session: `random.uniform(10, 50)` percent — simulating a vehicle arriving with a partially depleted battery.

## Temperature Derating

`EnvironmentConditions.power_derating_factor()` returns a multiplier based on ambient temperature:

| Temperature | Factor | Effect |
|---|---|---|
| ≤ -10°C | 0.40 | 40% of rated power (battery chemistry limited) |
| -10 to 0°C | 0.40 → 0.70 | Linear interpolation |
| 0 to 10°C | 0.70 → 0.90 | Linear interpolation |
| 10 to 35°C | 1.00 | Full rated power |
| 35 to 45°C | 1.00 → 0.60 | Thermal derating to protect cooling |
| ≥ 45°C | 0.50 | 50% — significant thermal derating |

Ramp duration also extends in cold weather:
- ≥ 10°C: 30s
- 0–10°C: 60s
- < 0°C: 90s

## V2G — Bidirectional Power Flow

V2G (Vehicle-to-Grid) allows the vehicle to export power back to the grid.

### Activation

```python
charge_state.direction = PowerDirection.DISCHARGING
charge_state.v2g.enabled = True
charge_state.v2g.max_discharge_kw = 50.0
charge_state.v2g.min_soc_floor = 20.0  # Never discharge below 20%
```

### Discharge Behavior

```python
def compute_discharge_power_kw(self) -> float:
    if self.soc <= self.v2g.min_soc_floor:
        return 0.0   # SoC floor reached — stop discharging
    discharge_kw = self.v2g.max_discharge_kw
    discharge_kw *= self.env.power_derating_factor()
    discharge_kw *= self.hardware_derating
    return max(0.0, discharge_kw * random.uniform(0.98, 1.02))
```

When direction is `DISCHARGING`:
- Energy is subtracted from SoC
- `discharge_meter_wh` accumulates exported energy separately from `meter_wh`
- If SoC reaches `min_soc_floor`, direction switches to `IDLE` automatically

### MeterValues in V2G Mode

During discharge:
- `Power.Active.Import` reports the import power (0 or near-0 during discharge)
- `Power.Active.Export` reports the export power
- `Energy.Active.Export.Register` tracks cumulative exported energy

### Smart Charging Profile Integration

If a `ChargingProfileLimit` with a negative value is active, the physics model interprets it as a V2G command:

```python
profile_limit = self.get_effective_limit_kw()
if profile_limit < 0:
    # Negative limit = discharge request from smart charging
    discharge_kw = min(abs(profile_limit), self.v2g.max_discharge_kw)
```

## Hardware Derating

Hardware faults (from `EnvironmentSimulator`) apply an additional derating multiplier:

```python
# Fan failure: 50% derating
fault = EnvironmentSimulator.make_fan_failure()
# → charge_state.hardware_derating *= 0.5
```

Multiple faults stack multiplicatively. A brownout (0.7) + fan failure (0.5) = 35% of rated power.

## MeterSnapshot

Each physics `tick(dt_sec)` returns a `MeterSnapshot`:

```python
@dataclass(frozen=True)
class MeterSnapshot:
    power_w: float           # Charge power (W)
    power_export_w: float    # Discharge/export power (W)
    energy_wh: float         # Cumulative charge energy (Wh)
    energy_export_wh: float  # Cumulative export energy (Wh)
    current_a: float         # Current (A) = power / voltage
    voltage: float           # Voltage (V) ≈ 400V ± 5%
    soc: float               # State of charge (%)
    power_kw: float          # Charge power (kW)
    discharge_kw: float      # Discharge power (kW)
    net_power_kw: float      # Net power (positive = charging, negative = discharging)
    elapsed_sec: float       # Session duration (s)
    direction: str           # "charging", "discharging", or "idle"
```

This snapshot maps directly to OCPP MeterValues measurands.

## Example: Session from 20% to 80% SoC

For a 75kWh battery at room temperature with a 120kW charger:

- Bulk phase energy: (80% - 20%) × 75kWh = 45kWh
- Time at full power (120kW): 45kWh / 120kW = 22.5 minutes
- Total with ramp: ≈24 minutes to 80%
- Taper to 95%: additional ≈10 minutes at declining power
- Trickle to 100%: additional ≈3 minutes
