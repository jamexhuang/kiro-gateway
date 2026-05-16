# -*- coding: utf-8 -*-

"""Tests for cumulative usage statistics."""
import pytest

from kiro.metrics import UsageStatsRegistry


class TestUsageStatsRegistry:
    def test_initial_snapshot_is_empty(self):
        r = UsageStatsRegistry()
        snap = r.snapshot()
        assert snap["total"] == {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "payload_bytes": 0,
        }
        assert snap["by_model"] == {}
        assert "since" in snap

    def test_record_completion_increments_totals(self):
        r = UsageStatsRegistry()
        r.record_completion("claude-opus-4.5", input_tokens=100, output_tokens=50, payload_bytes=2048)
        r.record_completion("claude-opus-4.5", input_tokens=200, output_tokens=80, payload_bytes=4096)
        snap = r.snapshot()
        assert snap["total"]["requests"] == 2
        assert snap["total"]["input_tokens"] == 300
        assert snap["total"]["output_tokens"] == 130
        assert snap["total"]["payload_bytes"] == 6144

    def test_record_completion_accumulates_by_model(self):
        r = UsageStatsRegistry()
        r.record_completion("claude-opus-4.5", 100, 50, 1000)
        r.record_completion("claude-haiku-4.5", 200, 80, 2000)
        r.record_completion("claude-opus-4.5", 300, 120, 3000)
        snap = r.snapshot()
        by_m = snap["by_model"]
        assert by_m["claude-opus-4.5"]["requests"] == 2
        assert by_m["claude-opus-4.5"]["input_tokens"] == 400
        assert by_m["claude-opus-4.5"]["output_tokens"] == 170
        assert by_m["claude-opus-4.5"]["payload_bytes"] == 4000
        assert by_m["claude-haiku-4.5"]["requests"] == 1
        assert by_m["claude-haiku-4.5"]["input_tokens"] == 200

    def test_record_completion_treats_none_as_zero(self):
        r = UsageStatsRegistry()
        r.record_completion("claude-opus-4.5", None, None, None)
        snap = r.snapshot()
        assert snap["total"]["requests"] == 1
        assert snap["total"]["input_tokens"] == 0
        assert snap["total"]["output_tokens"] == 0
        assert snap["total"]["payload_bytes"] == 0

    def test_record_completion_skips_empty_model(self):
        """An empty or None model name should still count the request but in a fallback bucket."""
        r = UsageStatsRegistry()
        r.record_completion("", 10, 20, 30)
        snap = r.snapshot()
        assert snap["total"]["requests"] == 1
        assert "(unknown)" in snap["by_model"]
        assert snap["by_model"]["(unknown)"]["requests"] == 1
