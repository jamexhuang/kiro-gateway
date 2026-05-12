# -*- coding: utf-8 -*-

"""
Unit tests for runtime control panel routing and dashboard APIs.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kiro.config import PROXY_API_KEY
from kiro.control_panel import ControlPanelState
from kiro.routes_dashboard import router as dashboard_router
from kiro.control_panel import control_panel


class TestControlPanelRouting:
    """Tests for runtime model routing decisions."""

    def test_default_routing_keeps_requested_model(self):
        """
        What it does: Verifies disabled runtime routing does not rewrite models.
        Purpose: Ensure existing projects keep current behavior by default.
        """
        print("Setup: Creating fresh control panel state...")
        state = ControlPanelState()

        print("Action: Routing claude-sonnet-4.5 with defaults...")
        decision = state.route_model("claude-sonnet-4.5")

        print(f"Decision: {decision}")
        assert decision.routed_model == "claude-sonnet-4.5"
        assert decision.applied is False
        assert decision.mode == "disabled"

    def test_manual_routing_uses_safe_original_fallback(self):
        """
        What it does: Enables manual routing and checks fallback order.
        Purpose: Ensure failed dashboard changes can retry the original client model.
        """
        print("Setup: Enabling manual routing from Opus 4.7 to Opus 4.6...")
        state = ControlPanelState()
        state.update_routing_config(
            {
                "enabled": True,
                "mode": "manual",
                "manual_model": "claude-opus-4.6",
                "fallback_models": ["claude-opus-4.6"],
                "safe_fallback_to_original": True,
            }
        )

        print("Action: Routing client request for claude-opus-4-7...")
        decision = state.route_model("claude-opus-4-7")

        print(f"Decision: {decision}")
        assert decision.routed_model == "claude-opus-4.6"
        assert decision.applied is True
        assert decision.fallback_models == ["claude-opus-4-7"]

    def test_opus_routing_filters_lower_quality_fallbacks(self):
        """
        What it does: Configures Opus routing with Sonnet and Haiku fallbacks.
        Purpose: Prevent automatic Opus downgrade while still allowing account failover.
        """
        print("Setup: Enabling manual Opus routing with lower-tier fallbacks...")
        state = ControlPanelState()
        state.update_routing_config(
            {
                "enabled": True,
                "mode": "manual",
                "manual_model": "claude-opus-4.6",
                "fallback_models": ["claude-sonnet-4.5", "claude-haiku-4.5"],
                "safe_fallback_to_original": False,
            }
        )

        print("Action: Routing client request for claude-opus-4-7...")
        decision = state.route_model("claude-opus-4-7")

        print(f"Decision: {decision}")
        assert decision.routed_model == "claude-opus-4.6"
        assert decision.fallback_models == []

    def test_redirect_routing_matches_normalized_model_name(self):
        """
        What it does: Verifies redirect rules apply after model normalization.
        Purpose: Support 4-7 and 4.7 style client model names.
        """
        print("Setup: Enabling redirect routing...")
        state = ControlPanelState()
        state.update_routing_config(
            {
                "enabled": True,
                "mode": "redirect",
                "redirects": {"claude-opus-4.7": "claude-opus-4.6"},
            }
        )

        print("Action: Routing dash-format claude-opus-4-7...")
        decision = state.route_model("claude-opus-4-7")

        print(f"Decision: {decision}")
        assert decision.routed_model == "claude-opus-4.6"
        assert decision.applied is True
        assert "Redirect rule matched" in decision.reason

    def test_should_retry_only_for_model_failure_statuses(self):
        """
        What it does: Checks model failure retry status classification.
        Purpose: Retry capacity/rate-limit model failures without retrying arbitrary server errors.
        """
        print("Setup: Creating fresh control panel state...")
        state = ControlPanelState()

        print("Action: Checking retryable and non-retryable statuses...")
        assert state.should_retry_failure(404, ["claude-opus-4.6"]) is True
        assert state.should_retry_failure(429, ["claude-opus-4.6"]) is True
        assert state.should_retry_failure(500, ["claude-opus-4.6"]) is False
        assert state.should_retry_failure(404, []) is False


class TestControlPanelMonitoring:
    """Tests for in-memory request monitoring."""

    def test_capture_content_disabled_keeps_payloads_empty(self):
        """
        What it does: Records a payload while capture_content is disabled.
        Purpose: Ensure sensitive content is not stored unless explicitly enabled.
        """
        print("Setup: Starting monitored request with default capture disabled...")
        state = ControlPanelState()
        decision = state.route_model("claude-sonnet-4.5")
        request_id = state.start_request("openai", "/v1/chat/completions", False, decision)

        print("Action: Recording payload and completing request...")
        state.record_payload(request_id, "client_request", {"messages": ["secret"]})
        state.finish_request(request_id, "completed")

        snapshot = state.snapshot()
        record = snapshot["completed_requests"][0]
        print(f"Completed record: {record}")
        assert record["payloads"] == {}

    def test_capture_content_stores_bounded_payload_and_response(self):
        """
        What it does: Enables content capture and records request/response data.
        Purpose: Ensure dashboard can inspect passing content without unbounded memory use.
        """
        print("Setup: Enabling content capture with small limit...")
        state = ControlPanelState()
        state.update_routing_config({"capture_content": True, "max_content_chars": 1000})
        decision = state.route_model("claude-sonnet-4.5")
        request_id = state.start_request("anthropic", "/v1/messages", True, decision)

        print("Action: Recording payload, stream chunk, and final response...")
        state.record_payload(request_id, "client_request", {"messages": ["hello"]})
        state.start_attempt(request_id, "claude-sonnet-4.5", "account-1")
        state.finish_attempt(request_id, 200)
        state.record_chunk(request_id, "data: hello")
        state.finish_request(request_id, "completed", response={"ok": True})

        snapshot = state.snapshot()
        record = snapshot["completed_requests"][0]
        print(f"Completed record: {record}")
        assert "client_request" in record["payloads"]
        assert record["chunks"] == ["data: hello"]
        assert '"ok": true' in record["response"]
        assert record["attempts"][0]["http_status"] == 200


class TestDashboardRoutes:
    """Tests for dashboard route authentication and state updates."""

    def setup_method(self):
        """
        Reset singleton state before each dashboard API test.
        """
        print("Resetting dashboard singleton state...")
        control_panel.reset_routing_config()
        control_panel.clear_monitor()

    def teardown_method(self):
        """
        Reset singleton state after each dashboard API test.
        """
        print("Cleaning dashboard singleton state...")
        control_panel.reset_routing_config()
        control_panel.clear_monitor()

    def _client(self) -> TestClient:
        """
        Create a small test app with only dashboard routes.

        Returns:
            TestClient for dashboard routes.
        """
        app = FastAPI()
        app.include_router(dashboard_router)
        return TestClient(app)

    def test_dashboard_page_loads_without_api_key(self):
        """
        What it does: Loads the static dashboard shell without auth.
        Purpose: Ensure only data APIs require secrets.
        """
        print("Action: GET /dashboard...")
        with self._client() as client:
            response = client.get("/dashboard")

        print(f"Status: {response.status_code}")
        assert response.status_code == 200
        assert "Kiro Gateway 控制台" in response.text
        assert "panel-routing" in response.text

    def test_state_api_without_authentication_returns_sanitized_state(self):
        """
        What it does: Calls dashboard state API without a key.
        Purpose: Avoid unauthenticated dashboard polling 401 spam while protecting monitoring data.
        """
        print("Action: GET /dashboard/api/state without auth...")
        with self._client() as client:
            response = client.get("/dashboard/api/state")

        print(f"Status: {response.status_code}")
        assert response.status_code == 200
        assert response.json()["authenticated"] is False
        assert response.json()["active_requests"] == []
        assert response.json()["completed_requests"] == []

    def test_routing_update_changes_runtime_state(self):
        """
        What it does: Updates routing through the dashboard API.
        Purpose: Ensure live model routing can be controlled without restart.
        """
        print("Action: PUT /dashboard/api/routing with valid key...")
        with self._client() as client:
            response = client.put(
                "/dashboard/api/routing",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                json={
                    "enabled": True,
                    "mode": "manual",
                    "manual_model": "claude-opus-4.7",
                    "safe_fallback_to_original": True,
                },
            )

        print(f"Result: {response.json()}")
        assert response.status_code == 200
        assert response.json()["routing"]["enabled"] is True
        assert response.json()["routing"]["manual_model"] == "claude-opus-4.7"

        decision = control_panel.route_model("claude-sonnet-4.5")
        print(f"Decision after API update: {decision}")
        assert decision.routed_model == "claude-opus-4.7"

    def test_routing_test_endpoint_reports_current_routing_decision(self):
        """
        What it does: Previews model routing through the dashboard API.
        Purpose: Provide a direct success test for dashboard routing changes.
        """
        print("Setup: Enabling manual routing through API...")
        with self._client() as client:
            update_response = client.put(
                "/dashboard/api/routing",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                json={
                    "enabled": True,
                    "mode": "manual",
                    "manual_model": "claude-opus-4.6",
                    "fallback_models": ["claude-sonnet-4.5"],
                    "safe_fallback_to_original": False,
                },
            )
            preview_response = client.post(
                "/dashboard/api/routing/test",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                json={"model": "claude-opus-4-7"},
            )

        print(f"Update: {update_response.json()}")
        print(f"Preview: {preview_response.json()}")
        assert update_response.status_code == 200
        assert preview_response.status_code == 200
        assert preview_response.json()["decision"]["original_model"] == "claude-opus-4-7"
        assert preview_response.json()["decision"]["routed_model"] == "claude-opus-4.6"
        assert preview_response.json()["decision"]["fallback_models"] == []


def test_record_metrics_and_trim_populate_record():
    from kiro.control_panel import ControlPanelState, RoutingDecision
    cp = ControlPanelState()
    decision = RoutingDecision(
        original_model="claude-opus-4-7",
        routed_model="claude-opus-4-7",
        applied=False,
        mode="disabled",
        reason="off",
        fallback_models=[],
    )
    rid = cp.start_request("anthropic", "/v1/messages", True, decision)
    cp.record_metrics(rid, ttft=0.28, tps=22.3, total_s=1.18, output_tokens=20, input_tokens=None)
    cp.record_trim(rid, before=280, after=78, before_bytes=5083328, after_bytes=305852)
    cp.finish_request(rid, "completed")

    snap = cp.snapshot()
    rec = snap["completed_requests"][0]
    assert rec["ttft_s"] == 0.28
    assert rec["tps"] == 22.3
    assert rec["output_tokens"] == 20
    assert rec["trim_before_messages"] == 280
    assert rec["trim_after_messages"] == 78
    assert rec["trim_before_bytes"] == 5083328
