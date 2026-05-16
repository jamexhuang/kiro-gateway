# -*- coding: utf-8 -*-

"""Rolling-window request metrics for dashboard sparklines."""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Deque, Dict, List, Tuple


class MetricsRegistry:
    """Ring of per-second buckets (count, errors, durations) over a window."""

    def __init__(self, window_s: int = 300, bucket_s: int = 5):
        self._window_s = window_s
        self._bucket_s = bucket_s
        self._lock = RLock()
        self._buckets: Deque[Tuple[int, List[float], int, int]] = deque()

    def _bucket_key(self, ts: float) -> int:
        return int(ts // self._bucket_s) * self._bucket_s

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()

    def record(self, ts: float, duration_s: float, is_error: bool) -> None:
        key = self._bucket_key(ts)
        with self._lock:
            self._prune(ts)
            if self._buckets and self._buckets[-1][0] == key:
                bucket = self._buckets[-1]
                bucket[1].append(duration_s)
                new_count = bucket[2] + 1
                new_err = bucket[3] + (1 if is_error else 0)
                self._buckets[-1] = (bucket[0], bucket[1], new_count, new_err)
            else:
                self._buckets.append((key, [duration_s], 1, 1 if is_error else 0))

    def snapshot(self, now: float) -> Dict[str, object]:
        with self._lock:
            self._prune(now)
            durations: List[float] = []
            count = 0
            errors = 0
            series: List[Dict[str, float]] = []
            for key, ds, c, e in self._buckets:
                durations.extend(ds)
                count += c
                errors += e
                series.append({"t": key, "count": c, "errors": e})
        if durations:
            sd = sorted(durations)
            p50 = sd[len(sd) // 2]
            p95 = sd[min(len(sd) - 1, int(len(sd) * 0.95))]
        else:
            p50 = 0.0
            p95 = 0.0
        return {
            "window_s": self._window_s,
            "bucket_s": self._bucket_s,
            "count": count,
            "errors": errors,
            "p50": p50,
            "p95": p95,
            "series": series,
        }


metrics_registry = MetricsRegistry(window_s=300, bucket_s=5)


class StageMetricsRegistry:
    """
    Per-stage rolling-window registry for latency breakdown.

    Each bucket holds a dict of stage_name -> list of durations (ms). Used by
    the dashboard "Latency Breakdown" panel to show P50/P95 per stage over the
    rolling window.
    """

    def __init__(self, window_s: int = 300, bucket_s: int = 5):
        self._window_s = window_s
        self._bucket_s = bucket_s
        self._lock = RLock()
        # bucket_key -> {stage_name: [durations_ms]}
        self._buckets: Deque[Tuple[int, Dict[str, List[float]]]] = deque()

    def _bucket_key(self, ts: float) -> int:
        return int(ts // self._bucket_s) * self._bucket_s

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()

    def record(self, ts: float, stages_ms: Dict[str, float]) -> None:
        if not stages_ms:
            return
        key = self._bucket_key(ts)
        with self._lock:
            self._prune(ts)
            if self._buckets and self._buckets[-1][0] == key:
                bucket = self._buckets[-1][1]
            else:
                bucket = {}
                self._buckets.append((key, bucket))
            for name, ms in stages_ms.items():
                if ms is None or ms < 0:
                    continue
                bucket.setdefault(name, []).append(float(ms))

    def snapshot(self, now: float) -> Dict[str, object]:
        with self._lock:
            self._prune(now)
            agg: Dict[str, List[float]] = {}
            series: Dict[str, List[Dict[str, float]]] = {}
            for key, stages in self._buckets:
                for name, vals in stages.items():
                    agg.setdefault(name, []).extend(vals)
                    series.setdefault(name, []).append({"t": key, "count": len(vals)})

        per_stage: Dict[str, Dict[str, object]] = {}
        total_count = 0
        for name, vals in agg.items():
            if not vals:
                continue
            sd = sorted(vals)
            p50 = sd[len(sd) // 2]
            p95 = sd[min(len(sd) - 1, int(len(sd) * 0.95))]
            per_stage[name] = {
                "count": len(vals),
                "p50": round(p50, 2),
                "p95": round(p95, 2),
                "series": series.get(name, []),
            }
            total_count = max(total_count, len(vals))
        return {
            "window_s": self._window_s,
            "bucket_s": self._bucket_s,
            "count": total_count,
            "per_stage": per_stage,
        }


stage_metrics_registry = StageMetricsRegistry(window_s=300, bucket_s=5)


import time as _time
from typing import Any, Optional


class UsageStatsRegistry:
    """Process-lifetime cumulative usage counters with per-model breakdown.

    Session-scoped (resets on process restart). All increments under a lock for thread safety.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._since: float = _time.time()
        self._total_requests: int = 0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_payload_bytes: int = 0
        self._by_model: Dict[str, Dict[str, int]] = {}

    def record_completion(
        self,
        model: Optional[str],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        payload_bytes: Optional[int],
    ) -> None:
        """Increment cumulative counters. None values are treated as 0."""
        i = int(input_tokens) if input_tokens else 0
        o = int(output_tokens) if output_tokens else 0
        b = int(payload_bytes) if payload_bytes else 0
        key = model if model else "(unknown)"
        with self._lock:
            self._total_requests += 1
            self._total_input_tokens += i
            self._total_output_tokens += o
            self._total_payload_bytes += b
            bucket = self._by_model.setdefault(
                key, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "payload_bytes": 0}
            )
            bucket["requests"] += 1
            bucket["input_tokens"] += i
            bucket["output_tokens"] += o
            bucket["payload_bytes"] += b

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "since": self._since,
                "total": {
                    "requests": self._total_requests,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                    "payload_bytes": self._total_payload_bytes,
                },
                "by_model": {k: dict(v) for k, v in self._by_model.items()},
            }


usage_stats_registry = UsageStatsRegistry()
