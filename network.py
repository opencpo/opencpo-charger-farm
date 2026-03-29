"""
Network degradation layer.
Sits between the virtual charger and the WebSocket, injecting latency,
packet loss, jitter, and connectivity failures.
"""

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FLAPPING = "flapping"
    ONE_WAY = "one_way"       # Can send, can't receive
    STALE = "stale"           # TCP alive, no data


@dataclass
class NetworkConfig:
    """Per-charger network degradation settings."""
    latency_ms: float = 0.0          # Base latency on all outgoing messages
    jitter_ms: float = 0.0           # Random ± jitter on top of latency
    packet_loss_pct: float = 0.0     # 0-100, chance message is silently dropped
    max_messages_per_sec: float = 0   # 0 = unlimited
    enabled: bool = False            # Master switch for degradation

    def effective_delay(self) -> float:
        """Return delay in seconds, including jitter."""
        if not self.enabled:
            return 0.0
        base = self.latency_ms / 1000.0
        jitter = random.uniform(-self.jitter_ms, self.jitter_ms) / 1000.0
        return max(0.0, base + jitter)

    def should_drop(self) -> bool:
        """Return True if this message should be dropped (packet loss)."""
        if not self.enabled or self.packet_loss_pct <= 0:
            return False
        return random.random() * 100 < self.packet_loss_pct

    def as_dict(self) -> dict:
        return {
            "latency_ms": self.latency_ms,
            "jitter_ms": self.jitter_ms,
            "packet_loss_pct": self.packet_loss_pct,
            "max_messages_per_sec": self.max_messages_per_sec,
            "enabled": self.enabled,
        }


@dataclass
class OfflineBuffer:
    """Stores messages that would have been sent during offline period."""
    messages: deque = field(default_factory=lambda: deque(maxlen=1000))
    meter_values: list = field(default_factory=list)

    def queue_meter_value(self, mv: dict) -> None:
        """Queue a MeterValue with its original timestamp for replay on reconnect."""
        self.meter_values.append(mv)

    def drain_meter_values(self) -> list:
        """Return and clear all queued meter values."""
        values = list(self.meter_values)
        self.meter_values.clear()
        return values

    @property
    def queued_count(self) -> int:
        return len(self.meter_values)


class NetworkLayer:
    """
    Network degradation layer for a single charger.
    Wraps send operations with latency, loss, and throttling.
    """

    def __init__(self, cp_id: str):
        self.cp_id = cp_id
        self.config = NetworkConfig()
        self.state = ConnectionState.CONNECTED
        self.offline_buffer = OfflineBuffer()

        # Rate limiting state
        self._send_times: deque = deque()
        self._rate_window = 1.0  # 1 second window

        # Connectivity event tracking
        self.disconnect_time: Optional[float] = None
        self.reconnect_count: int = 0
        self.total_dropped: int = 0
        self.total_delayed: int = 0

    async def send(self, ws, message: str) -> bool:
        """
        Send a message through the degradation layer.
        Returns True if message was sent, False if dropped.
        """
        if self.state in (ConnectionState.DISCONNECTED, ConnectionState.STALE):
            return False

        if self.config.enabled:
            # Packet loss
            if self.config.should_drop():
                self.total_dropped += 1
                log.debug("[%s] Message dropped (packet loss)", self.cp_id)
                return False

            # Rate limiting
            if self.config.max_messages_per_sec > 0:
                now = time.monotonic()
                while self._send_times and self._send_times[0] < now - self._rate_window:
                    self._send_times.popleft()
                if len(self._send_times) >= self.config.max_messages_per_sec:
                    log.debug("[%s] Message throttled", self.cp_id)
                    return False
                self._send_times.append(now)

            # Latency
            delay = self.config.effective_delay()
            if delay > 0:
                self.total_delayed += 1
                await asyncio.sleep(delay)

        # One-way mode: send succeeds but we pretend responses don't arrive
        await ws.send(message)
        return True

    async def receive(self, ws) -> Optional[str]:
        """Receive a message through the degradation layer."""
        if self.state == ConnectionState.ONE_WAY:
            # In one-way mode, we never receive anything
            # Block forever (until cancelled)
            await asyncio.Future()
            return None

        msg = await ws.recv()

        if self.config.enabled and self.config.should_drop():
            self.total_dropped += 1
            log.debug("[%s] Incoming message dropped", self.cp_id)
            return None

        return msg

    def go_offline(self) -> None:
        self.state = ConnectionState.DISCONNECTED
        self.disconnect_time = time.monotonic()

    def go_online(self) -> None:
        self.state = ConnectionState.CONNECTED
        self.reconnect_count += 1
        self.disconnect_time = None

    def set_flapping(self) -> None:
        self.state = ConnectionState.FLAPPING

    def set_one_way(self) -> None:
        self.state = ConnectionState.ONE_WAY

    def set_stale(self) -> None:
        self.state = ConnectionState.STALE

    @property
    def offline_duration_sec(self) -> float:
        if self.disconnect_time is None:
            return 0.0
        return time.monotonic() - self.disconnect_time

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "config": self.config.as_dict(),
            "offline_duration_sec": round(self.offline_duration_sec, 1),
            "reconnect_count": self.reconnect_count,
            "total_dropped": self.total_dropped,
            "total_delayed": self.total_delayed,
            "queued_messages": self.offline_buffer.queued_count,
        }
