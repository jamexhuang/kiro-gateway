# -*- coding: utf-8 -*-

"""Unit tests for kiro.metrics.StageMetricsRegistry."""

import time


class TestStageMetricsRegistry:
    """Tests for the rolling-window per-stage registry."""

    def test_record_and_snapshot_basic(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry(window_s=60, bucket_s=1)
        now = time.time()
        for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            reg.record(now, {"auth_ms": ms})
        snap = reg.snapshot(now)
        assert "auth_ms" in snap["per_stage"]
        s = snap["per_stage"]["auth_ms"]
        assert s["count"] == 10
        # P50 of [10..100] is index 5 (ms=60) in this 0-indexed scheme
        assert 40 <= s["p50"] <= 70
        assert s["p95"] >= s["p50"]

    def test_snapshot_drops_old_buckets(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry(window_s=2, bucket_s=1)
        t0 = 1_000_000.0
        reg.record(t0, {"auth_ms": 50.0})
        snap = reg.snapshot(t0 + 10)
        assert snap["per_stage"] == {}
        assert snap["count"] == 0

    def test_empty_registry(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry()
        snap = reg.snapshot(time.time())
        assert snap["per_stage"] == {}
        assert snap["count"] == 0
        assert snap["window_s"] == 300

    def test_record_empty_dict_noop(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry()
        reg.record(time.time(), {})
        snap = reg.snapshot(time.time())
        assert snap["per_stage"] == {}

    def test_record_filters_none_and_negative(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry()
        now = time.time()
        reg.record(now, {"auth_ms": None, "gate_wait_ms": -5.0, "ttft_ms": 100.0})
        snap = reg.snapshot(now)
        assert "auth_ms" not in snap["per_stage"]
        assert "gate_wait_ms" not in snap["per_stage"]
        assert "ttft_ms" in snap["per_stage"]

    def test_multiple_stages_independent(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry()
        now = time.time()
        for ms in [10, 20, 30]:
            reg.record(now, {"auth_ms": ms, "ttft_ms": ms * 10})
        snap = reg.snapshot(now)
        assert snap["per_stage"]["auth_ms"]["count"] == 3
        assert snap["per_stage"]["ttft_ms"]["count"] == 3
        assert snap["per_stage"]["ttft_ms"]["p50"] > snap["per_stage"]["auth_ms"]["p50"]

    def test_singleton_instance_exists(self):
        from kiro.metrics import stage_metrics_registry, StageMetricsRegistry

        assert isinstance(stage_metrics_registry, StageMetricsRegistry)

    def test_series_field_per_stage(self):
        from kiro.metrics import StageMetricsRegistry

        reg = StageMetricsRegistry(window_s=60, bucket_s=1)
        now = time.time()
        reg.record(now, {"auth_ms": 5.0})
        snap = reg.snapshot(now)
        s = snap["per_stage"]["auth_ms"]
        assert isinstance(s["series"], list)
        assert len(s["series"]) == 1
        assert s["series"][0]["count"] == 1
