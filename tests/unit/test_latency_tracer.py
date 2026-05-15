# -*- coding: utf-8 -*-

"""Unit tests for kiro.latency_tracer."""

import time

import pytest


class TestLatencyTrace:
    """Tests for the LatencyTrace dataclass."""

    def test_to_dict_contains_all_stage_keys(self):
        from kiro.latency_tracer import LatencyTrace

        t = LatencyTrace(auth_ms=10.0, ttft_ms=50.0)
        d = t.to_dict()
        assert "auth_ms" in d
        assert "gate_wait_ms" in d
        assert "stages" in d
        assert d["auth_ms"] == 10.0
        assert d["gate_wait_ms"] is None

    def test_per_stage_durations_filters_none(self):
        from kiro.latency_tracer import LatencyTrace

        t = LatencyTrace(auth_ms=10.0, gate_wait_ms=None, ttft_ms=20.0)
        d = t.per_stage_durations()
        assert d == {"auth_ms": 10.0, "ttft_ms": 20.0}

    def test_per_stage_durations_filters_negative(self):
        from kiro.latency_tracer import LatencyTrace

        t = LatencyTrace(auth_ms=-5.0, ttft_ms=20.0)
        d = t.per_stage_durations()
        assert "auth_ms" not in d
        assert d["ttft_ms"] == 20.0


class TestLatencyTracer:
    """Tests for the per-request LatencyTracer."""

    def test_add_records_duration_in_ms(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("req-1", t0=1000.0)
        tracer.add("auth_ms", 0.05, start_ts=1000.0)
        trace = tracer.finalize()
        assert trace.auth_ms == 50.0
        assert len(trace.stages) == 1
        assert trace.stages[0]["name"] == "auth_ms"
        assert trace.stages[0]["duration_ms"] == 50.0

    def test_add_negative_duration_ignored(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.add("auth_ms", -1.0)
        assert tracer.finalize().auth_ms is None

    def test_add_none_duration_ignored(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.add("auth_ms", None)
        assert tracer.finalize().auth_ms is None

    def test_finalize_computes_streaming_from_total_minus_ttft(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("req-1", t0=1000.0)
        tracer.set_ttft(0.5)
        tracer.set_total(2.0)
        trace = tracer.finalize()
        assert trace.ttft_ms == 500.0
        assert trace.total_ms == 2000.0
        assert trace.streaming_ms == 1500.0

    def test_finalize_streaming_clamped_at_zero(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.set_ttft(2.0)
        tracer.set_total(1.0)  # Total < ttft (clock skew edge case)
        trace = tracer.finalize()
        assert trace.streaming_ms == 0.0

    def test_finalize_idempotent(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.add("auth_ms", 0.01)
        t1 = tracer.finalize()
        t2 = tracer.finalize()
        assert t1.auth_ms == t2.auth_ms == 10.0

    def test_add_accumulates_same_stage(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.add("auth_ms", 0.01)
        tracer.add("auth_ms", 0.02)
        # Two recorded calls accumulate
        assert tracer.finalize().auth_ms == 30.0

    def test_set_ttft_negative_ignored(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.set_ttft(-1.0)
        tracer.set_total(1.0)
        trace = tracer.finalize()
        assert trace.ttft_ms is None

    def test_per_stage_durations_only_populated_stages(self):
        from kiro.latency_tracer import LatencyTracer

        tracer = LatencyTracer("r")
        tracer.add("auth_ms", 0.01)
        tracer.add("gate_wait_ms", 0.005)
        d = tracer.finalize().per_stage_durations()
        assert "auth_ms" in d
        assert "gate_wait_ms" in d
        assert "upstream_connect_ms" not in d


class TestNoOpTracer:
    """Tests for the NoOp tracer used when LATENCY_TRACING is disabled."""

    def test_noop_returns_empty_trace(self):
        from kiro.latency_tracer import _NoOpTracer

        tracer = _NoOpTracer()
        tracer.add("auth_ms", 1.0)
        tracer.set_ttft(1.0)
        tracer.set_total(2.0)
        trace = tracer.finalize()
        assert trace.auth_ms is None
        assert trace.total_ms is None
        assert trace.stages == []

    def test_get_tracer_returns_singleton_when_disabled(self, monkeypatch):
        from kiro.latency_tracer import get_tracer, _NOOP_SINGLETON

        monkeypatch.setattr("kiro.latency_tracer._is_enabled", lambda: False)
        t1 = get_tracer("r1")
        t2 = get_tracer("r2")
        assert t1 is _NOOP_SINGLETON
        assert t2 is _NOOP_SINGLETON

    def test_get_tracer_returns_real_when_enabled(self, monkeypatch):
        from kiro.latency_tracer import get_tracer, LatencyTracer, _NOOP_SINGLETON

        monkeypatch.setattr("kiro.latency_tracer._is_enabled", lambda: True)
        t = get_tracer("r1")
        assert t is not _NOOP_SINGLETON
        assert isinstance(t, LatencyTracer)
