# -*- coding: utf-8 -*-

"""
Tests for the dashboard passkey authentication routes and the unified
session-or-API-key authorization dependency.
"""

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from kiro.config import PROXY_API_KEY
from kiro import routes_dashboard_auth as auth_mod
from kiro.dashboard_auth import DashboardAuthStore, ChallengeStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A test app with a fresh auth store and a dummy protected route."""
    store = DashboardAuthStore(path=str(tmp_path / "dashboard_auth.json"))
    challenges = ChallengeStore(ttl_seconds=300)
    monkeypatch.setattr(auth_mod, "dashboard_auth_store", store)
    monkeypatch.setattr(auth_mod, "dashboard_challenges", challenges)
    # TestClient speaks http://testserver; align origin so cookies are non-Secure
    # and thus round-trip in the test transport (prod uses https).
    monkeypatch.setattr(auth_mod, "DASHBOARD_ORIGIN", "http://testserver")

    app = FastAPI()
    app.include_router(auth_mod.auth_router)

    @app.get("/protected")
    async def _protected(_: bool = Depends(auth_mod.require_dashboard_auth)):
        return {"ok": True}

    return TestClient(app), store, challenges


class TestAuthStatus:
    def test_status_unregistered_unauthenticated(self, client):
        c, store, _ = client
        r = c.get("/dashboard/api/auth/status")
        assert r.status_code == 200
        assert r.json() == {"registered": False, "authenticated": False}


class TestProtectedDependency:
    def test_no_credentials_rejected(self, client):
        c, _, _ = client
        assert c.get("/protected").status_code == 401

    def test_api_key_accepted(self, client):
        c, _, _ = client
        r = c.get("/protected", headers={"x-api-key": PROXY_API_KEY})
        assert r.status_code == 200

    def test_valid_session_cookie_accepted(self, client):
        c, store, _ = client
        token = store.issue_session(now=_frozen_now())
        c.cookies.set("kiro_dash_session", token)
        # Freeze time so the freshly issued token is still valid.
        r = c.get("/protected")
        assert r.status_code == 200

    def test_bogus_session_cookie_rejected(self, client):
        c, _, _ = client
        c.cookies.set("kiro_dash_session", "garbage.value")
        assert c.get("/protected").status_code == 401


class TestRegistrationGating:
    def test_bootstrap_requires_api_key(self, client, monkeypatch):
        c, store, challenges = client
        monkeypatch.setattr(auth_mod.svc, "begin_registration",
                            lambda *a, **k: ("flow-1", {"rp": {"id": "localhost"}, "challenge": "x"}))
        # No key, unregistered → blocked
        assert c.post("/dashboard/api/auth/register/begin", json={}).status_code == 403
        # With key, unregistered → allowed (bootstrap)
        r = c.post("/dashboard/api/auth/register/begin", json={},
                   headers={"x-api-key": PROXY_API_KEY})
        assert r.status_code == 200
        assert r.json()["flow_id"] == "flow-1"

    def test_registered_blocks_api_key_only_registration(self, client, monkeypatch):
        c, store, _ = client
        store.add_credential(credential_id="c1", public_key="pk", sign_count=0,
                             transports=[], nickname="k")
        monkeypatch.setattr(auth_mod.svc, "begin_registration",
                            lambda *a, **k: ("flow-2", {}))
        # API key alone must NOT allow adding a passkey once registered
        assert c.post("/dashboard/api/auth/register/begin", json={},
                      headers={"x-api-key": PROXY_API_KEY}).status_code == 403


class TestLoginFlow:
    def test_successful_login_sets_session_and_authenticates(self, client, monkeypatch):
        c, store, _ = client
        store.add_credential(credential_id="c1", public_key="pk", sign_count=0,
                             transports=[], nickname="k")
        monkeypatch.setattr(auth_mod.svc, "begin_authentication",
                            lambda *a, **k: ("flow-x", {"challenge": "x"}))
        monkeypatch.setattr(auth_mod.svc, "complete_authentication",
                            lambda *a, **k: True)

        c.post("/dashboard/api/auth/login/begin", json={})
        r = c.post("/dashboard/api/auth/login/complete",
                   json={"flow_id": "flow-x", "credential": {"id": "c1"}})
        assert r.status_code == 200
        assert "kiro_dash_session" in r.cookies
        # The issued cookie now authorizes the protected route.
        assert c.get("/protected").status_code == 200

    def test_failed_login_no_session(self, client, monkeypatch):
        c, store, _ = client
        store.add_credential(credential_id="c1", public_key="pk", sign_count=0,
                             transports=[], nickname="k")
        monkeypatch.setattr(auth_mod.svc, "complete_authentication",
                            lambda *a, **k: False)
        r = c.post("/dashboard/api/auth/login/complete",
                   json={"flow_id": "bad", "credential": {"id": "c1"}})
        assert r.status_code == 401
        assert "kiro_dash_session" not in r.cookies


class TestAccountCRUDEndpoints:
    def test_list_accounts_unauthenticated_rejected(self, client):
        c, _, _ = client
        assert c.get("/dashboard/api/accounts").status_code == 401

    def test_list_accounts_authenticated_success(self, client, monkeypatch):
        c, store, _ = client
        # Setup mock account manager in app.state
        mock_manager = MagicMock()
        mock_manager.get_accounts_snapshot.return_value = [{"id": "acc-1", "display_id": "Acc 1"}]
        c.app.state.account_manager = mock_manager

        # Set session cookie to bypass authentication
        token = store.issue_session(now=_frozen_now())
        c.cookies.set("kiro_dash_session", token)

        r = c.get("/dashboard/api/accounts")
        assert r.status_code == 200
        assert r.json() == {"accounts": [{"id": "acc-1", "display_id": "Acc 1"}]}
        mock_manager.get_accounts_snapshot.assert_called_once()

    def test_create_account_success(self, client, monkeypatch):
        c, store, _ = client
        mock_manager = AsyncMock()
        mock_manager.add_account_entry.return_value = "acc-new"
        c.app.state.account_manager = mock_manager

        token = store.issue_session(now=_frozen_now())
        c.cookies.set("kiro_dash_session", token)

        r = c.post("/dashboard/api/accounts", json={"type": "refresh_token", "refresh_token": "token"})
        assert r.status_code == 200
        assert r.json() == {"success": True, "account_id": "acc-new"}
        mock_manager.add_account_entry.assert_called_once()

    def test_delete_account_success(self, client, monkeypatch):
        c, store, _ = client
        mock_manager = AsyncMock()
        mock_manager.remove_account_entry.return_value = True
        c.app.state.account_manager = mock_manager

        token = store.issue_session(now=_frozen_now())
        c.cookies.set("kiro_dash_session", token)

        r = c.delete("/dashboard/api/accounts/acc-1")
        assert r.status_code == 200
        assert r.json() == {"success": True}
        mock_manager.remove_account_entry.assert_called_once_with("acc-1")

    def test_patch_account_success(self, client, monkeypatch):
        c, store, _ = client
        mock_manager = AsyncMock()
        mock_manager.update_account_entry.return_value = True
        c.app.state.account_manager = mock_manager

        token = store.issue_session(now=_frozen_now())
        c.cookies.set("kiro_dash_session", token)

        r = c.patch("/dashboard/api/accounts/acc-1", json={"disabled": True, "comment": "new"})
        assert r.status_code == 200
        assert r.json() == {"success": True}
        mock_manager.update_account_entry.assert_called_once_with("acc-1", disabled=True, comment="new")


def _frozen_now() -> float:
    """A fixed timestamp used so issued tokens stay valid during the test."""
    import time
    return time.time()
