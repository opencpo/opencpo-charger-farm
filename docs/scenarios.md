# Stress Test Scenarios

The charger farm includes 17 built-in stress test scenarios. Each scenario spawns a set of virtual chargers and drives them through a specific pattern to test server behavior under load.

## Intensity Levels

All scenarios accept an `intensity` parameter that determines how many chargers to spawn:

| Intensity | Charger Count |
|---|---|
| `low` | 5 |
| `medium` | 20 |
| `high` | 50 |
| `extreme` | 100 |

Override with `--count N` to use a specific number regardless of intensity.

---

## 1. `ramp_up`

**What it tests:** How the server handles chargers connecting one at a time over a period.

**Behavior:** Spawns chargers sequentially with a configurable interval (default 2s). Each charger connects, boots, and starts a charging session.

**Expected behavior:** Server should accept all connections without dropping messages or degrading response times as count increases. Useful for finding memory leaks and connection handling issues at steady-state growth.

**Parameters:** `interval_sec` (default 2.0), `hold_sec` (default 30), `target` (override count)

---

## 2. `peak_load`

**What it tests:** Server behavior when all chargers connect simultaneously.

**Behavior:** All chargers spawn at once, then all start charging at once 5 seconds later. This creates a thundering-herd scenario for both WebSocket connections and database writes.

**Expected behavior:** Server should handle the burst without rejecting connections. BootNotification responses should arrive within a few seconds for all chargers. Database writes may be queued but should complete.

**Parameters:** `count` (override intensity count), `hold_sec` (default 60)

---

## 3. `disconnect_storm`

**What it tests:** Server's handling of random disconnections on active sessions.

**Behavior:** Spawns chargers, starts charging sessions on all of them, then randomly disconnects a percentage (default 50%). Disconnected chargers auto-reconnect after a few seconds.

**Expected behavior:** Sessions interrupted by disconnect should remain in `active` state (not prematurely marked `completed`). Server should handle reconnect flood gracefully.

**Parameters:** `count`, `pct` (default 50 — percentage disconnected), `delay` (default 1.0s between disconnects)

---

## 4. `reconnect_flood`

**What it tests:** Simultaneous reconnect of all chargers.

**Behavior:** All chargers disconnect at the same time, then auto-reconnect simultaneously. This simulates a power outage at a site or a CSMS restart.

**Expected behavior:** Server should accept all reconnections without dropping messages. BootNotifications should be processed even when hundreds arrive within milliseconds. 

---

## 5. `mixed_sessions`

**What it tests:** Interleaved start/stop of charging sessions at random intervals.

**Behavior:** Over a configurable duration, randomly picks a charger and either starts or stops its session (60% start, 40% stop). Simulates normal usage patterns with no fixed order.

**Expected behavior:** Session state should remain consistent — no orphaned sessions, no double-starts, correct energy accounting.

**Parameters:** `count`, `duration` (default 120s)

---

## 6. `firmware_quirks`

**What it tests:** Server robustness against chargers with known non-standard behavior.

**Behavior:** Spawns all chargers with the MAXPOWER quirk profile enabled. All quirks simultaneously: StopTransaction(reason=Other) on reconnect, energy in non-standard units, slow status notifications.

**Expected behavior:** Server should handle quirks without crashing or producing incorrect session records.

---

## 7. `session_persistence`

**What it tests:** Session state integrity across different types of disconnection.

**Behavior:** Spawns chargers, starts sessions, then runs one of four patterns:
- `clean` — StopTransaction then disconnect (normal flow)
- `dirty` — disconnect without StopTransaction (network failure)
- `flap` — rapid connect/disconnect cycles (5 rounds)
- `long_outage` — all disconnect and stay offline for 60s

**Expected behavior:** After reconnect, sessions should be in the correct state. Dirty disconnects should leave sessions `active` until explicitly stopped. Clean disconnects should finalize sessions.

**Parameters:** `count`, `pattern` (default `flap`)

---

## 8. `v2g_peak_shaving`

**What it tests:** Bidirectional power flow — all chargers exporting to the grid simultaneously.

**Behavior:** Starts charging sessions, waits for SoC to build up, then triggers V2G discharge on all chargers at the same power level. Simulates a grid peak shaving event.

**Expected behavior:** Server should correctly handle negative energy flow in MeterValues. Exported energy should be tracked separately from consumed energy.

**Parameters:** `count`, `kw` (default 50kW per charger), `duration` (default 60s)

---

## 9. `v2g_solar_storage`

**What it tests:** Charge-then-discharge cycle simulating solar storage.

**Behavior:** Two phases — first charges all vehicles (simulating midday solar surplus), then discharges them (simulating evening demand). 30 seconds each phase in test mode.

**Expected behavior:** Server handles direction changes cleanly. No session errors during direction flip. Energy accounting correct for both import and export.

---

## 10. `v2g_frequency_regulation`

**What it tests:** Rapid alternating charge/discharge cycles.

**Behavior:** Runs N cycles of 5 seconds discharge + 5 seconds charge. Simulates a charger enrolled in frequency regulation services where it must respond to grid frequency deviations rapidly.

**Expected behavior:** Server handles rapid state changes without session corruption or message loss.

**Parameters:** `count`, `cycles` (default 10)

---

## 11. `chaos`

**What it tests:** Random unpredictable behavior — the "everything is broken" scenario.

**Behavior:** Randomly applies any of these actions to random chargers at random intervals: disconnect, error injection, start session, stop session, V2G start, V2G stop. The interval between events decreases with higher intensity.

**Expected behavior:** Server should never crash. Sessions may be in inconsistent states after chaos, but the server itself must remain operational and responsive.

**Parameters:** `count`, `duration` (default 120s), `chaos_intensity` (default = `intensity`)

---

## 12. `winter_stress`

**What it tests:** Cold weather charging behavior.

**Behavior:** Sets ambient temperature to -10°C for all chargers, then starts charging sessions. Cold batteries have reduced charging power and longer ramp times (90s vs 30s at room temperature).

**Expected behavior:** Reduced power levels (down to 40% at -10°C) should flow correctly through MeterValues. Server should not flag reduced power as an error.

**Parameters:** `count`, `hold_sec` (default 60)

---

## 13. `summer_peak`

**What it tests:** Hot weather thermal derating.

**Behavior:** Sets ambient temperature to 42°C, starts all chargers. At high temperature, chargers derate power to protect cooling systems.

**Expected behavior:** Power readings significantly below rated capacity. Server handles variable power levels correctly.

**Parameters:** `count`, `hold_sec` (default 60)

---

## 14. `network_hell`

**What it tests:** Degraded network conditions — latency, jitter, and packet loss all at once.

**Behavior:** Each charger gets random network degradation applied: latency 500–5000ms, jitter 100–1000ms, packet loss 10–50%. Starts charging sessions through this degraded connection.

**Expected behavior:** Server should handle delayed messages without timing out sessions prematurely. Dropped messages should be retried by the charger. Session accounting should remain correct despite delays.

**Parameters:** `count`, `hold_sec` (default 60)

---

## 15. `site_power_event`

**What it tests:** Cascading power outage — chargers losing power one after another.

**Behavior:** All chargers disconnect in cascade with short random delays (0.1–0.5s between each). After 15 seconds, they all start reconnecting (simulating power restoration).

**Expected behavior:** Server handles large numbers of disconnections and reconnections within a short window. No message loss. Sessions remain intact after power restoration.

**Parameters:** `count`, `hold_sec` (default 45)

---

## 16. `endurance`

**What it tests:** Long-running stability — no crashes, no memory leaks, correct behavior over time.

**Behavior:** Runs for a configurable number of hours. Every 30 seconds, applies a random event to one charger: restart session, force disconnect, or nothing. The rest of the time, chargers just charge normally.

**Expected behavior:** Server memory and CPU usage should remain stable. No error rate increase over time. Session accounting should remain accurate throughout.

**Parameters:** `count`, `hours` (default 1)

---

## 17. `pnc_flow`

**What it tests:** Plug & Charge certificate exchange flow (OCPP 2.0.1 + ISO 15118).

**Behavior:** Spawns chargers with PnC config enabled. Each charger triggers the PnC certificate provisioning flow: generates a key pair, sends a CSR, receives a signed certificate.

**Expected behavior:** Server's PKI CA should correctly sign CSRs and return valid certificates. The full ISO 15118 handshake should complete without errors.

**Parameters:** `count`, `hold_sec` (default 30)
