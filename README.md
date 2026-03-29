# Virtual Charger Farm

OCPP stress testing tool for EV charging infrastructure. Spawns virtual chargers that connect to your OCPP central system over WebSocket, simulating real Hongjiali/MAXPOWER hardware with accurate firmware quirks.

## Quick Start

### Python

```bash
pip install -r requirements.txt
python control.py
```

Open [http://localhost:8086](http://localhost:8086) in your browser.

### Docker

```bash
docker compose up --build
```

## Features

- **OCPP 1.6j and 2.0.1** virtual chargers with full message flows
- **Hongjiali/MAXPOWER firmware quirks** — WebSocket ping=0, string MeterValues, StopTransaction-on-reconnect, ConnectionTimeOut rejection
- **Real charge curves** — SoC-dependent power ramp, temperature derating, V2G discharge simulation
- **17 stress test scenarios** — ramp-up, peak load, disconnect storms, reconnect floods, V2G cycling, chaos, endurance, and more
- **Network degradation** — configurable latency, jitter, packet loss per charger or globally
- **Environmental simulation** — temperature, grid voltage, power outages, vehicle behavior faults
- **Plug & Charge (ISO 15118)** simulation
- **Live web UI** — dark themed control panel with real-time metrics, charger management, and scenario execution
- **Automated reports** — JSON + branded HTML reports with performance metrics, session integrity checks, and vulnerability assessment

## Charger Profiles

| Profile | Power | Connectors | OCPP | Description |
|---------|-------|------------|------|-------------|
| ENC-DCL120B | 120 kW | 2x CCS2 | 2.0.1 | Production DC fast charger |
| ENC-DCL120B-16 | 120 kW | 2x CCS2 | 1.6j | Lab/legacy DC fast charger |
| ENC-DCL060B | 60 kW | 2x CCS2 | 1.6j | Mid-range DC charger |
| ENC-DCX030A | 30 kW | 1x CCS2 | 1.6j | Portable DC charger |

## Scenarios

| Scenario | Description |
|----------|-------------|
| `ramp_up` | Spawn chargers one by one at steady interval |
| `peak_load` | All chargers connect and charge simultaneously |
| `disconnect_storm` | Random WebSocket disconnects on active chargers |
| `reconnect_flood` | All chargers disconnect then reconnect at once |
| `mixed_sessions` | Staggered start/stop of charging sessions |
| `firmware_quirks` | All chargers with MAXPOWER quirks enabled |
| `session_persistence` | Test session persistence across disconnect patterns |
| `v2g_peak_shaving` | All chargers discharge simultaneously |
| `v2g_solar_storage` | Charge during day, discharge in evening |
| `v2g_frequency_regulation` | Rapid charge/discharge cycles |
| `chaos` | Random real-world events at varying intensity |
| `winter_stress` | Cold weather charging (-10C) |
| `summer_peak` | Hot weather derating (42C) |
| `network_hell` | Every network problem simultaneously |
| `site_power_event` | Cascading power outage |
| `endurance` | Long-running test with periodic random events |
| `pnc_flow` | Plug & Charge certification test |

Each scenario supports intensity levels: `low` (5 chargers), `medium` (20), `high` (50), `extreme` (100).

## Configuration

Environment variables (or set via UI Settings):

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8086` | HTTP port |
| `OCPP16_URL` | `ws://localhost:9100/ocpp` | OCPP 1.6j server WebSocket URL |
| `OCPP201_URL` | `ws://localhost:9201/ocpp` | OCPP 2.0.1 server WebSocket URL |
| `CPO_API_URL` | _(empty)_ | CPO API for session validation in reports |
| `REDIS_HOST` | _(empty)_ | Redis host for stale key checks |

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | Farm overview (counts, msg rate, latency) |
| `GET` | `/api/chargers` | List all chargers with status |
| `POST` | `/api/chargers` | Spawn charger(s) |
| `DELETE` | `/api/chargers/{cp_id}` | Stop a charger |
| `POST` | `/api/chargers/{cp_id}/start` | Start charging session |
| `POST` | `/api/chargers/{cp_id}/stop` | Stop charging session |
| `POST` | `/api/chargers/{cp_id}/disconnect` | Force WebSocket disconnect |
| `POST` | `/api/chargers/{cp_id}/reconnect` | Force reconnect |
| `POST` | `/api/chargers/{cp_id}/error` | Inject error |
| `POST` | `/api/chargers/{cp_id}/v2g` | Start/stop V2G discharge |
| `POST` | `/api/chargers/{cp_id}/pnc` | Trigger Plug & Charge |
| `GET` | `/api/scenarios` | List scenarios |
| `POST` | `/api/scenarios/{name}` | Run a scenario |
| `DELETE` | `/api/scenarios/active` | Stop running scenario |
| `GET` | `/api/metrics` | Current metrics snapshot |
| `GET` | `/api/metrics/stream` | SSE metrics stream |
| `GET` | `/api/events` | Event log |
| `GET` | `/api/profiles` | Charger profiles |
| `GET` | `/api/settings` | Current settings |
| `PUT` | `/api/settings` | Update settings |
| `GET` | `/api/environment` | Environment state |
| `PUT` | `/api/environment` | Update environment |
| `GET` | `/api/reports` | List reports |
| `GET` | `/api/reports/{id}` | Get report (JSON or HTML) |

## UI Screenshots

_Coming soon._

## Project Structure

```
virtual-charger/
  control.py          # FastAPI control plane (entrypoint)
  charger16.py        # OCPP 1.6j virtual charger
  charger201.py       # OCPP 2.0.1 virtual charger
  profiles.py         # Hongjiali product profiles
  physics.py          # Charge/discharge curves
  metrics.py          # In-memory metrics + SSE
  network.py          # Network degradation simulation
  environment.py      # Environmental fault simulation
  pnc.py              # Plug & Charge simulation
  scenarios.py        # Stress test scenarios
  reports.py          # Report generator
  static/index.html   # Web UI
  reports/            # Generated reports
```

## Contributing

1. Fork the repo
2. Create a feature branch
3. Make your changes (don't break existing Python modules)
4. Test with `python control.py` and verify the UI at `:8086`
5. Submit a PR

## License

Apache 2.0 — see [LICENSE](LICENSE).
