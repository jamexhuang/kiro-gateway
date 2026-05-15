# -*- coding: utf-8 -*-

"""
Latency tracing for end-to-end request observability.

Default-OFF. When LATENCY_TRACING_ENABLED is False, get_tracer() returns a
NoOp singleton whose methods are no-ops, keeping hot path overhead negligible.

Stages:
  - auth_ms              token acquisition (KiroAuthManager.get_access_token)
  - gate_wait_ms         AdaptiveGate.acquire() wait time
  - upstream_connect_ms  client.send -> response status_code received
  - ttft_ms              first token (mirrors record.ttft_s)
  - streaming_ms         first token -> last chunk
  - total_ms             request_started -> request_finished
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from threading import Lock
from typing import Any, Dict, List, Optional


def _is_enabled() -> bool:
    """Lazy-read config so tests can monkeypatch the env-derived flag."""
    try:
        from kiro.config import LATENCY_TRACING_ENABLED
        return bool(LATENCY_TRACING_ENABLED)
    except Exception:
        return False


@dataclass
class LatencyTrace:
    """Snapshot of one request's latency breakdown (ms)."""
    auth_ms: Optional[float] = None
    gate_wait_ms: Optional[float] = None
    upstream_connect_ms: Optional[float] = None
    ttft_ms: Optional[float] = None
    streaming_ms: Optional[float] = None
    total_ms: Optional[float] = None
    # Each stage entry: {"name": str, "start_offset_ms": float, "duration_ms": float}
    stages: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def per_stage_durations(self) -> Dict[str, float]:
        """Return dict of stage_name -> duration_ms for non-None stages."""
        out: Dict[str, float] = {}
        for k in ("auth_ms", "gate_wait_ms", "upstream_connect_ms", "ttft_ms", "streaming_ms"):
            v = getattr(self, k)
            if v is not None and v >= 0:
                out[k] = v
        return out


class LatencyTracer:
    """Per-request tracer. Thread-safe via internal Lock."""

    __slots__ = ("_request_id", "_t0", "_lock", "_durations", "_stages",
                 "_finalized", "_ttft_s", "_total_s")

    def __init__(self, request_id: str, t0: Optional[float] = None):
        self._request_id = request_id
        self._t0 = t0 if t0 is not None else time.monotonic()
        self._lock = Lock()
        self._durations: Dict[str, float] = {}
        self._stages: List[Dict[str, Any]] = []
        self._finalized = False
        self._ttft_s: Optional[float] = None
        self._total_s: Optional[float] = None

    def add(self, stage: str, duration_s: float, start_ts: Optional[float] = None) -> None:
        """Record a stage duration. If start_ts is given, captures stage offset."""
        if duration_s is None or duration_s < 0:
            return
        ms = duration_s * 1000.0
        with self._lock:
            self._durations[stage] = self._durations.get(stage, 0.0) + ms
            if start_ts is not None:
                offset_ms = (start_ts - self._t0) * 1000.0
                self._stages.append({
                    "name": stage,
                    "start_offset_ms": round(offset_ms, 2),
                    "duration_ms": round(ms, 2),
                })

    def set_ttft(self, ttft_s: float) -> None:
        if ttft_s is None or ttft_s < 0:
            return
        with self._lock:
            self._ttft_s = ttft_s

    def set_total(self, total_s: float) -> None:
        if total_s is None or total_s < 0:
            return
        with self._lock:
            self._total_s = total_s

    def finalize(self) -> LatencyTrace:
        """Compute final trace. Safe to call multiple times."""
        with self._lock:
            d = self._durations
            ttft_ms = self._ttft_s * 1000.0 if self._ttft_s is not None else None
            total_ms = self._total_s * 1000.0 if self._total_s is not None else None
            streaming_ms: Optional[float] = None
            if total_ms is not None and ttft_ms is not None:
                streaming_ms = max(0.0, total_ms - ttft_ms)
            self._finalized = True
            return LatencyTrace(
                auth_ms=round(d["auth_ms"], 2) if "auth_ms" in d else None,
                gate_wait_ms=round(d["gate_wait_ms"], 2) if "gate_wait_ms" in d else None,
                upstream_connect_ms=round(d["upstream_connect_ms"], 2) if "upstream_connect_ms" in d else None,
                ttft_ms=round(ttft_ms, 2) if ttft_ms is not None else None,
                streaming_ms=round(streaming_ms, 2) if streaming_ms is not None else None,
                total_ms=round(total_ms, 2) if total_ms is not None else None,
                stages=list(self._stages),
            )


class _NoOpTracer:
    """Zero-overhead no-op tracer used when latency tracing is disabled."""

    __slots__ = ()

    def add(self, stage: str, duration_s: float, start_ts: Optional[float] = None) -> None:
        return

    def set_ttft(self, ttft_s: float) -> None:
        return

    def set_total(self, total_s: float) -> None:
        return

    def finalize(self) -> LatencyTrace:
        return LatencyTrace()


_NOOP_SINGLETON = _NoOpTracer()


def get_tracer(request_id: str, t0: Optional[float] = None):
    """Factory: real tracer when enabled, NoOp singleton otherwise."""
    if _is_enabled():
        return LatencyTracer(request_id=request_id, t0=t0)
    return _NOOP_SINGLETON


def is_enabled() -> bool:
    return _is_enabled()
