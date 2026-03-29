# Configuration

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OCPP_URL` | `ws://localhost:9100/ocpp/VIRT-001` | CSMS WebSocket URL |
| `CP_ID` | `VIRT-001` | Charger ID (appears in URL path) |
| `VENDOR` | `VirtualCharger` | Vendor name in BootNotification |
| `MODEL` | `Virtual-120kW` | Model name in BootNotification |
| `SERIAL` | `{CP_ID}-SN` | Serial number in BootNotification |
| `FIRMWARE` | `VIRT-1.0.0` | Firmware version in BootNotification |
| `MAX_KW` | `120` | Rated maximum charging power (kW) |
| `NUM_CONNECTORS` | `2` | Number of simulated connectors |
| `INITIAL_SOC` | `0` | Battery SoC at session start (%) |
| `HEARTBEAT_INTERVAL` | `30` | Heartbeat interval (seconds) |
| `METER_INTERVAL` | `30` | MeterValues sample interval (seconds) |
| `AUTO_START` | `false` | Start charging immediately after boot |
| `RECONNECT_DELAY` | `5` | Seconds to wait before reconnecting after disconnect |

## Control API

The farm runs a control plane API for managing virtual chargers:

| Variable | Default | Description |
|---|---|---|
| `CONTROL_HOST` | `0.0.0.0` | Control API bind address |
| `CONTROL_PORT` | `8090` | Control API port |

## Farm API Reference

```bash
# List all connected chargers
GET /chargers

# Spawn a new charger
POST /chargers
{
  "cp_id": "STRESS-001",
  "profile": "ENC-DCL120B-16",
  "ocpp_version": "1.6"
}

# Stop a charger
DELETE /chargers/{cp_id}

# Start charging on a charger
POST /chargers/{cp_id}/start-charging
{"connector_id": 1, "id_tag": "TEST001"}

# Stop charging
POST /chargers/{cp_id}/stop-charging
{"reason": "Local"}

# Trigger V2G discharge
POST /chargers/{cp_id}/v2g/start
{"max_discharge_kw": 50}

# Stop V2G
POST /chargers/{cp_id}/v2g/stop

# Force disconnect (without StopTransaction)
POST /chargers/{cp_id}/disconnect

# Inject a hardware fault
POST /chargers/{cp_id}/fault
{"fault_type": "HighTemperature", "connector_id": 0, "duration_sec": 120}

# Apply network degradation
PUT /chargers/{cp_id}/network
{
  "enabled": true,
  "latency_ms": 500,
  "jitter_ms": 200,
  "packet_loss_pct": 5
}

# Run a scenario
POST /scenarios/{scenario_name}/run
{"intensity": "medium", "count": 20, "hold_sec": 60}

# Get scenario status
GET /scenarios/{scenario_name}/status

# Stop running scenario
POST /scenarios/{scenario_name}/cancel

# Get live metrics
GET /metrics

# Site environment
PUT /site/temperature
{"ambient_temp_c": -10.0}
```

## Charger Profiles

The farm uses the same charger profile system as ocpp-core. Built-in profiles:

| Profile ID | Description | Max Power |
|---|---|---|
| `generic-ac-22kw` | Generic AC charger | 22 kW |
| `generic-dc-50kw` | Generic DC fast charger | 50 kW |
| `generic-dc-120kw` | Generic DC ultra-fast | 120 kW |
| `maxpower-dc` | DC charger with MAXPOWER quirks | 120 kW |

Set via `POST /chargers` with `"profile": "profile-id"`.

## Quirks

The `MAXPOWER_QUIRKS` preset can be applied to simulate known real-world charger issues:

- Sends `StopTransaction(reason=Other)` on reconnect
- Reports energy in kWh instead of Wh
- Slow to send `StatusNotification` after boot

Apply via scenario or API:
```bash
curl -X POST http://localhost:8090/chargers \
  -d '{"cp_id":"QUIRK-001", "quirks": "maxpower"}'
```

## Docker

Run the farm in Docker:

```bash
docker run -e OCPP_URL=ws://host.docker.internal:9100/ocpp/VIRT-001 \
           -e CP_ID=VIRT-001 \
           -e MAX_KW=120 \
           -p 8090:8090 \
           ocpp-charger-farm
```

Or with docker-compose to run multiple chargers:

```yaml
services:
  farm:
    image: ocpp-charger-farm
    environment:
      OCPP_URL: ws://ocpp-core:9100/ocpp/
      CONTROL_PORT: 8090
    ports:
      - "8090:8090"
```
