"""
In-memory metrics collection for the virtual charger farm.
Rolling window counters, latency tracking, SSE broadcasting.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class LatencySample:
    timestamp: float
    latency_ms: float


class FarmMetrics:
    """Collects and exposes metrics for the charger farm."""

    def __init__(self, window_sec: float = 60.0):
        self._window_sec = window_sec

        # Counters (cumulative)
        self.total_messages_sent: int = 0
        self.total_messages_received: int = 0
        self.total_connections: int = 0
        self.total_disconnections: int = 0
        self.total_errors: int = 0
        self.total_sessions_started: int = 0
        self.total_sessions_ended: int = 0

        # Rolling window samples
        self._messages_sent: deque[float] = deque()
        self._messages_received: deque[float] = deque()
        self._connections: deque[float] = deque()
        self._disconnections: deque[float] = deque()
        self._errors: deque[float] = deque()
        self._latencies: deque[LatencySample] = deque()

        # SSE subscribers
        self._subscribers: list[asyncio.Queue] = []

        # Event log (last 500 events)
        self._event_log: deque[dict] = deque(maxlen=500)

        self.start_time: float = time.monotonic()

    def _prune(self, q: deque, now: float) -> None:
        cutoff = now - self._window_sec
        while q and q[0] < cutoff:
            q.popleft()

    def _prune_latencies(self, now: float) -> None:
        cutoff = now - self._window_sec
        while self._latencies and self._latencies[0].timestamp < cutoff:
            self._latencies.popleft()

    def record_message_sent(self) -> None:
        self.total_messages_sent += 1
        self._messages_sent.append(time.monotonic())

    def record_message_received(self) -> None:
        self.total_messages_received += 1
        self._messages_received.append(time.monotonic())

    def record_connection(self) -> None:
        self.total_connections += 1
        self._connections.append(time.monotonic())

    def record_disconnection(self) -> None:
        self.total_disconnections += 1
        self._disconnections.append(time.monotonic())

    def record_error(self) -> None:
        self.total_errors += 1
        self._errors.append(time.monotonic())

    def record_latency(self, latency_ms: float) -> None:
        self._latencies.append(LatencySample(time.monotonic(), latency_ms))

    def record_session_started(self) -> None:
        self.total_sessions_started += 1

    def record_session_ended(self) -> None:
        self.total_sessions_ended += 1

    def log_event(self, level: str, source: str, message: str) -> None:
        """Log an event and broadcast to SSE subscribers."""
        event = {
            "timestamp": time.time(),
            "level": level,
            "source": source,
            "message": message,
        }
        self._event_log.append(event)
        self._broadcast({"type": "event", "data": event})

    def _broadcast(self, data: dict) -> None:
        """Send data to all SSE subscribers."""
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to SSE stream. Returns a queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _rate(self, q: deque) -> float:
        now = time.monotonic()
        self._prune(q, now)
        if not q:
            return 0.0
        span = now - q[0] if len(q) > 1 else self._window_sec
        return len(q) / max(span, 1.0)

    def reset_counters(self) -> None:
        """Reset all cumulative counters and windowed samples."""
        self.total_messages_sent = 0
        self.total_messages_received = 0
        self.total_connections = 0
        self.total_disconnections = 0
        self.total_errors = 0
        self.total_sessions_started = 0
        self.total_sessions_ended = 0
        self._messages_sent.clear()
        self._messages_received.clear()
        self._connections.clear()
        self._disconnections.clear()
        self._errors.clear()
        self._latencies.clear()
        self.start_time = time.monotonic()
        self._event_log.clear()
        self.log_event("info", "farm", "Counters reset")

    def snapshot(self) -> dict:
        now = time.monotonic()
        self._prune(self._messages_sent, now)
        self._prune(self._messages_received, now)
        self._prune(self._connections, now)
        self._prune(self._disconnections, now)
        self._prune(self._errors, now)
        self._prune_latencies(now)

        avg_latency = 0.0
        if self._latencies:
            avg_latency = sum(s.latency_ms for s in self._latencies) / len(self._latencies)

        return {
            "uptime_sec": round(now - self.start_time, 1),
            "messages_per_sec": round(
                self._rate(self._messages_sent) + self._rate(self._messages_received), 2
            ),
            "messages_sent_per_sec": round(self._rate(self._messages_sent), 2),
            "messages_received_per_sec": round(self._rate(self._messages_received), 2),
            "connections_per_sec": round(self._rate(self._connections), 2),
            "avg_latency_ms": round(avg_latency, 1),
            "total_messages_sent": self.total_messages_sent,
            "total_messages_received": self.total_messages_received,
            "total_connections": self.total_connections,
            "total_disconnections": self.total_disconnections,
            "total_errors": self.total_errors,
            "total_sessions_started": self.total_sessions_started,
            "total_sessions_ended": self.total_sessions_ended,
        }

    def get_events(self, limit: int = 100) -> list[dict]:
        return list(self._event_log)[-limit:]

    def broadcast_metrics(self) -> None:
        """Push current metrics snapshot to all subscribers."""
        self._broadcast({"type": "metrics", "data": self.snapshot()})

    def reset(self) -> None:
        self.total_messages_sent = 0
        self.total_messages_received = 0
        self.total_connections = 0
        self.total_disconnections = 0
        self.total_errors = 0
        self.total_sessions_started = 0
        self.total_sessions_ended = 0
        self._messages_sent.clear()
        self._messages_received.clear()
        self._connections.clear()
        self._disconnections.clear()
        self._errors.clear()
        self._latencies.clear()
        self.start_time = time.monotonic()


# Global singleton
farm_metrics = FarmMetrics()
