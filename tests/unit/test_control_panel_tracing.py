# -*- coding: utf-8 -*-

"""Integration tests for ControlPanelState + LatencyTracer."""

import time


def _start_decision(state, model="claude-sonnet-4.5"):
    """Helper: produce a RoutingDecision for start_request()."""
    from kiro.control_panel import RoutingDecision

    return RoutingDecision(
        original_model=model,
        routed_model=model,
        applied=False,
        mode="disabled",
        reason="test",
        fallback_models=[],
    )


class TestControlPanelTracing:
    """Tests for trace integration in ControlPanelState."""

    def test_no_trace_when_disabled(self, monkeypatch):
        from kiro.control_panel import ControlPanelState

        monkeypatch.setattr("kiro.latency_tracer._is_enabled", lambda: False)
        cp = ControlPanelState()
        rid = cp.start_request("openai", "/v1/chat", False, _start_decision(cp))
        cp.add_trace_stage(rid, "auth_ms", 0.01)  # No-op when disabled
        cp.finish_request(rid, "completed")

        rec = list(cp._completed_requests)[0]
        assert rec.trace is None

    def test_trace_populated_when_enabled(self, monkeypatch):
        from kiro.control_panel import ControlPanelState

        monkeypatch.setattr("kiro.latency_tracer._is_enabled", lambda: True)
        cp = ControlPanelState()
        rid = cp.start_request("openai", "/v1/chat", True, _start_decision(cp))

        # Tracer should have been created
        assert rid in cp._tracers

        cp.add_trace_stage(rid, "auth_ms", 0.05)
        cp.add_trace_stage(rid, "gate_wait_ms", 0.02)
        cp.add_trace_stage(rid, "upstream_connect_ms", 0.10)

        # Simulate ttft via record_metrics
        cp.record_metrics(rid, ttft=0.3, total_s=2.0)
        cp.finish_request(rid, "completed")

        rec = list(cp._completed_requests)[0]
        assert rec.trace is not None
        assert rec.trace["auth_ms"] == 50.0
        assert rec.trace["gate_wait_ms"] == 20.0
        assert rec.trace["upstream_connect_ms"] == 100.0
        # ttft populated from record.ttft_s
        assert rec.trace["ttft_ms"] == 300.0
        # streaming = total - ttft
        assert rec.trace["streaming_ms"] is not None

    def test_tracer_cleaned_up_on_finish(self, monkeypatch):
        from kiro.control_panel import ControlPanelState

        monkeypatch.setattr("kiro.latency_tracer._is_enabled", lambda: True)
        cp = ControlPanelState()
        rid = cp.start_request("openai", "/v1/chat", False, _start_decision(cp))
        assert rid in cp._tracers
        cp.finish_request(rid, "completed")
        assert rid not in cp._tracers

    def test_add_trace_stage_unknown_request_safe(self):
        from kiro.control_panel import ControlPanelState

        cp = ControlPanelState()
        # Should not raise even if request_id is unknown
        cp.add_trace_stage("nonexistent", "auth_ms", 0.01)

    def test_finish_records_to_stage_metrics_registry(self, monkeypatch):
        from kiro.control_panel import ControlPanelState
        from kiro.metrics import stage_metrics_registry

        monkeypatch.setattr("kiro.latency_tracer._is_enabled", lambda: True)
        cp = ControlPanelState()
        rid = cp.start_request("openai", "/v1/chat", False, _start_decision(cp))
        cp.add_trace_stage(rid, "auth_ms", 0.05)
        cp.record_metrics(rid, ttft=0.2, total_s=1.0)

        before = stage_metrics_registry.snapshot(time.time()).get("per_stage", {}).get("auth_ms", {}).get("count", 0)
        cp.finish_request(rid, "completed")
        after = stage_metrics_registry.snapshot(time.time()).get("per_stage", {}).get("auth_ms", {}).get("count", 0)
        assert after >= before + 1
