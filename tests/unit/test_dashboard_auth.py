# -*- coding: utf-8 -*-

"""
Unit tests for the dashboard passkey authentication store and session signing.
"""

import json

import pytest

from kiro.dashboard_auth import DashboardAuthStore


class TestSessionSigning:
    """Tests for HMAC-signed stateless session cookies."""

    def _store(self, tmp_path) -> DashboardAuthStore:
        return DashboardAuthStore(path=str(tmp_path / "dashboard_auth.json"), session_ttl_seconds=3600)

    def test_issued_session_verifies(self, tmp_path):
        """
        What it does: Issues a session token and verifies it.
        Purpose: A freshly issued, untampered token must be accepted.
        """
        store = self._store(tmp_path)
        token = store.issue_session(now=1000.0)
        assert store.verify_session(token, now=1000.0) is True

    def test_tampered_session_rejected(self, tmp_path):
        """
        What it does: Flips a character in the signature and verifies.
        Purpose: Any tampering must invalidate the signature.
        """
        store = self._store(tmp_path)
        token = store.issue_session(now=1000.0)
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert store.verify_session(tampered, now=1000.0) is False

    def test_expired_session_rejected(self, tmp_path):
        """
        What it does: Verifies a token after its TTL has elapsed.
        Purpose: Expired sessions must not be accepted.
        """
        store = self._store(tmp_path)
        token = store.issue_session(now=1000.0)
        assert store.verify_session(token, now=1000.0 + 3601) is False

    def test_session_from_other_secret_rejected(self, tmp_path):
        """
        What it does: Verifies a token signed by a different store/secret.
        Purpose: Sessions must be bound to this server's secret.
        """
        store_a = self._store(tmp_path)
        store_b = DashboardAuthStore(path=str(tmp_path / "other.json"), session_ttl_seconds=3600)
        token = store_a.issue_session(now=1000.0)
        assert store_b.verify_session(token, now=1000.0) is False

    def test_garbage_token_rejected(self, tmp_path):
        """
        What it does: Verifies malformed inputs.
        Purpose: Must fail closed on junk, never raise.
        """
        store = self._store(tmp_path)
        for junk in ("", "....", "not-a-token", "a.b", None):
            assert store.verify_session(junk, now=1000.0) is False


class TestCredentialStorage:
    """Tests for passkey credential persistence and the bootstrap flag."""

    def _store(self, tmp_path):
        return DashboardAuthStore(path=str(tmp_path / "dashboard_auth.json"))

    def test_not_registered_when_empty(self, tmp_path):
        """No passkeys → is_registered False (bootstrap allowed)."""
        assert self._store(tmp_path).is_registered() is False

    def test_add_credential_marks_registered(self, tmp_path):
        """Adding a credential flips is_registered to True."""
        store = self._store(tmp_path)
        store.add_credential(credential_id="cred-1", public_key="pk", sign_count=0,
                             transports=["internal"], nickname="laptop")
        assert store.is_registered() is True

    def test_get_credential_returns_stored_fields(self, tmp_path):
        """A stored credential is retrievable by id with its public key."""
        store = self._store(tmp_path)
        store.add_credential(credential_id="cred-1", public_key="PUBKEY", sign_count=5,
                             transports=["usb"], nickname="key")
        cred = store.get_credential("cred-1")
        assert cred is not None
        assert cred["public_key"] == "PUBKEY"
        assert cred["sign_count"] == 5

    def test_get_unknown_credential_returns_none(self, tmp_path):
        """Unknown credential id returns None."""
        assert self._store(tmp_path).get_credential("nope") is None

    def test_list_credentials_hides_no_public_fields(self, tmp_path):
        """list_credentials exposes display fields, never the session secret."""
        store = self._store(tmp_path)
        store.add_credential(credential_id="cred-1", public_key="pk", sign_count=0,
                             transports=[], nickname="phone")
        listed = store.list_credentials()
        assert len(listed) == 1
        assert listed[0]["id"] == "cred-1"
        assert listed[0]["nickname"] == "phone"
        assert "public_key" not in listed[0]

    def test_update_sign_count_persists(self, tmp_path):
        """Updating the signature counter is saved (replay protection)."""
        store = self._store(tmp_path)
        store.add_credential(credential_id="cred-1", public_key="pk", sign_count=0,
                             transports=[], nickname="k")
        store.update_sign_count("cred-1", 42)
        assert store.get_credential("cred-1")["sign_count"] == 42

    def test_remove_credential(self, tmp_path):
        """Removing the only credential returns to unregistered state."""
        store = self._store(tmp_path)
        store.add_credential(credential_id="cred-1", public_key="pk", sign_count=0,
                             transports=[], nickname="k")
        assert store.remove_credential("cred-1") is True
        assert store.is_registered() is False
        assert store.remove_credential("cred-1") is False

    def test_state_persists_across_reload(self, tmp_path):
        """A new store instance reads credentials and the same session secret."""
        path = str(tmp_path / "dashboard_auth.json")
        store = DashboardAuthStore(path=path)
        store.add_credential(credential_id="cred-1", public_key="pk", sign_count=1,
                             transports=[], nickname="k")
        token = store.issue_session(now=1000.0)

        reloaded = DashboardAuthStore(path=path)
        assert reloaded.is_registered() is True
        assert reloaded.get_credential("cred-1")["sign_count"] == 1
        # Same secret survived reload, so the earlier token still verifies.
        assert reloaded.verify_session(token, now=1000.0) is True


class TestChallengeStore:
    """Tests for the in-memory single-use WebAuthn challenge store."""

    def test_created_challenge_can_be_consumed_once(self, tmp_path):
        from kiro.dashboard_auth import ChallengeStore
        cs = ChallengeStore(ttl_seconds=300)
        flow_id = cs.create(b"challenge-bytes", now=1000.0)
        assert cs.consume(flow_id, now=1000.0) == b"challenge-bytes"

    def test_challenge_is_single_use(self, tmp_path):
        from kiro.dashboard_auth import ChallengeStore
        cs = ChallengeStore(ttl_seconds=300)
        flow_id = cs.create(b"c", now=1000.0)
        cs.consume(flow_id, now=1000.0)
        assert cs.consume(flow_id, now=1000.0) is None

    def test_expired_challenge_returns_none(self, tmp_path):
        from kiro.dashboard_auth import ChallengeStore
        cs = ChallengeStore(ttl_seconds=300)
        flow_id = cs.create(b"c", now=1000.0)
        assert cs.consume(flow_id, now=1000.0 + 301) is None

    def test_unknown_flow_id_returns_none(self, tmp_path):
        from kiro.dashboard_auth import ChallengeStore
        cs = ChallengeStore(ttl_seconds=300)
        assert cs.consume("nope", now=1000.0) is None


class TestRegistrationGate:
    """Tests for whether passkey registration is permitted."""

    def _store(self, tmp_path):
        return DashboardAuthStore(path=str(tmp_path / "dashboard_auth.json"))

    def test_bootstrap_allowed_with_api_key_when_unregistered(self, tmp_path):
        """Zero passkeys + valid API key → bootstrap registration allowed."""
        store = self._store(tmp_path)
        assert store.may_register(has_valid_session=False, has_valid_api_key=True) is True

    def test_bootstrap_blocked_without_api_key_when_unregistered(self, tmp_path):
        """Zero passkeys + no key + no session → registration blocked (no public takeover)."""
        store = self._store(tmp_path)
        assert store.may_register(has_valid_session=False, has_valid_api_key=False) is False

    def test_additional_passkey_requires_session_when_registered(self, tmp_path):
        """Already registered: API key alone must NOT add passkeys; needs a session."""
        store = self._store(tmp_path)
        store.add_credential(credential_id="c", public_key="pk", sign_count=0, transports=[], nickname="k")
        assert store.may_register(has_valid_session=False, has_valid_api_key=True) is False
        assert store.may_register(has_valid_session=True, has_valid_api_key=False) is True


class TestWebAuthnService:
    """Tests for the WebAuthn ceremony service option generation."""

    def _ctx(self, tmp_path):
        from kiro.dashboard_auth import ChallengeStore
        store = DashboardAuthStore(path=str(tmp_path / "dashboard_auth.json"))
        return store, ChallengeStore(ttl_seconds=300)

    def test_begin_registration_returns_options_and_stores_challenge(self, tmp_path):
        from kiro import dashboard_auth_service as svc
        store, challenges = self._ctx(tmp_path)
        flow_id, options = svc.begin_registration(
            store, challenges, rp_id="localhost", rp_name="Kiro", origin="http://localhost", now=1000.0)
        assert options["rp"]["id"] == "localhost"
        assert "challenge" in options
        # The flow id resolves to a stored challenge (single-use).
        assert challenges.consume(flow_id, now=1000.0) is not None

    def test_begin_authentication_returns_options_and_stores_challenge(self, tmp_path):
        from kiro import dashboard_auth_service as svc
        store, challenges = self._ctx(tmp_path)
        store.add_credential(credential_id="Y3JlZA", public_key="pk", sign_count=0,
                             transports=["internal"], nickname="k")
        flow_id, options = svc.begin_authentication(
            store, challenges, rp_id="localhost", now=1000.0)
        assert "challenge" in options
        assert challenges.consume(flow_id, now=1000.0) is not None

    def test_complete_registration_rejects_expired_flow(self, tmp_path):
        from kiro import dashboard_auth_service as svc
        store, challenges = self._ctx(tmp_path)
        with pytest.raises(svc.WebAuthnFlowError):
            svc.complete_registration(store, challenges, rp_id="localhost", origin="http://localhost",
                                      flow_id="unknown", credential={}, nickname="k", now=1000.0)

    def test_complete_authentication_rejects_expired_flow(self, tmp_path):
        from kiro import dashboard_auth_service as svc
        store, challenges = self._ctx(tmp_path)
        assert svc.complete_authentication(store, challenges, rp_id="localhost",
                                           origin="http://localhost", flow_id="unknown",
                                           credential={}, now=1000.0) is False
