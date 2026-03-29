# Network Degradation

The virtual charger can simulate degraded network conditions between the charger and the CSMS. This is useful for testing how your server handles real-world connectivity problems like cellular links, congested WiFi, or long-distance connections.

## How It Works

Every charger has a `NetworkLayer` instance that intercepts outgoing and incoming messages before they hit the WebSocket. The layer can:

1. **Drop messages** (packet loss)
2. **Delay messages** (latency + jitter)
3. **Throttle throughput** (rate limiting)
4. **Simulate one-way connectivity** (can send, can't receive)
5. **Buffer messages while offline** (queued MeterValues)

## Configuration

```python
from network import NetworkConfig

config = NetworkConfig(
    enabled=True,
    latency_ms=200.0,       # Base delay on all outgoing messages
    jitter_ms=50.0,         # ±50ms random variation on top of latency
    packet_loss_pct=5.0,    # 5% of messages silently dropped
    max_messages_per_sec=0, # 0 = unlimited; set >0 to throttle
)
```

Via the control API:
```bash
curl -X PUT http://localhost:8090/chargers/VIRT-001/network \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "latency_ms": 500,
    "jitter_ms": 200,
    "packet_loss_pct": 10
  }'
```

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. No degradation when false |
| `latency_ms` | float | `0` | Base added latency on every sent message (ms) |
| `jitter_ms` | float | `0` | Random ±jitter on top of latency (ms) |
| `packet_loss_pct` | float | `0` | Percentage of messages randomly dropped (0–100) |
| `max_messages_per_sec` | float | `0` | Max messages per second; 0 = unlimited |

## Effective Delay Calculation

```
delay = latency_ms/1000 + random.uniform(-jitter_ms, +jitter_ms)/1000
delay = max(0, delay)  # never negative
```

At `latency_ms=200, jitter_ms=50`, each message is delayed between 150ms and 250ms.

## Packet Loss

Each message is independently evaluated against `packet_loss_pct`. At 10% loss:
- Each message has a 10% probability of being silently dropped
- Applies to both sent and received messages independently
- Lost messages are tracked in `network.total_dropped`

## Connection States

The network layer tracks the logical connection state:

| State | Description |
|---|---|
| `connected` | Normal operation |
| `disconnected` | No messages sent or received |
| `flapping` | Rapid alternation between connected and disconnected |
| `one_way` | Can send messages, but receives are blocked (simulates asymmetric routing) |
| `stale` | TCP connection alive but no data flows (simulates idle timeout issues) |

Set state via `force_disconnect()`, `go_online()`, `set_one_way()`, etc.

## Offline Message Buffering

When a charger disconnects (either by force or network failure), MeterValues that would have been sent are queued in `OfflineBuffer`:

```python
buffer = OfflineBuffer()
buffer.queue_meter_value({"measurand": ..., "value": ..., "timestamp": ...})

# On reconnect, drain the buffer and replay
queued = buffer.drain_meter_values()
for mv in queued:
    await send_meter_value(mv)
```

The buffer holds up to 1000 queued MeterValues (configurable). This tests how your server handles out-of-order or batched historical readings.

## Monitoring

```python
status = network_layer.status()
# {
#   "state": "connected",
#   "config": {
#     "latency_ms": 500,
#     "jitter_ms": 200,
#     "packet_loss_pct": 10,
#     "enabled": true
#   },
#   "offline_duration_sec": 0.0,
#   "reconnect_count": 3,
#   "total_dropped": 47,
#   "total_delayed": 312,
#   "queued_messages": 0
# }
```

## Scenarios That Use Network Degradation

### `network_hell`

Applies maximum degradation to all chargers simultaneously:
- Latency: 500–5000ms (random per charger)
- Jitter: 100–1000ms (random per charger)
- Packet loss: 10–50% (random per charger)

This creates the worst-case conditions you'd see on a saturated 4G cell or a long-distance satellite link. If your server can handle `network_hell`, it can handle anything.

### Per-Scenario Configuration

Any scenario can configure network degradation for its chargers:

```python
for cp_id in self._spawned_ids:
    charger = self.farm.get_charger(cp_id)
    charger.network.config.enabled = True
    charger.network.config.latency_ms = 1000
    charger.network.config.packet_loss_pct = 20
```

## Typical Values for Real-World Scenarios

| Scenario | Latency | Jitter | Loss |
|---|---|---|---|
| Good broadband | 5ms | 2ms | 0% |
| Office WiFi | 20ms | 10ms | 0.1% |
| Congested WiFi | 100ms | 80ms | 2% |
| Good 4G | 50ms | 20ms | 0.5% |
| Poor 4G | 300ms | 200ms | 5% |
| Weak 3G | 800ms | 500ms | 10% |
| Satellite | 600ms | 100ms | 1% |
