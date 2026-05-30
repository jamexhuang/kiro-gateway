# -*- coding: utf-8 -*-

"""
Dashboard passkey authentication store and stateless session signing.

This module backs the single-admin WebAuthn/FIDO2 login for the gateway
dashboard. It owns:

- Persistent storage (a small JSON file) of registered passkey credentials and
  a server-side session secret.
- Issuing and verifying HMAC-signed, stateless session cookies.

WebAuthn registration/assertion ceremony lives in the route layer; this store
holds the durable state those flows read and write.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

# Default lifetime of a dashboard session cookie.
DEFAULT_SESSION_TTL_SECONDS: int = 12 * 60 * 60


def _b64url_encode(raw: bytes) -> str:
    """Encode bytes as unpadded base64url text."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Decode unpadded base64url text back to bytes."""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class ChallengeStore:
    """
    In-memory, single-use, TTL-bounded store for WebAuthn challenges.

    A flow id is handed to the client when a ceremony begins; the client echoes
    it on completion so the server can recover (and consume) the matching
    challenge. Challenges are one-shot and expire to bound replay windows.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        """
        Args:
            ttl_seconds: Lifetime of an unconsumed challenge.
        """
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    def create(self, challenge: bytes, now: float) -> str:
        """Store a challenge and return its single-use flow id."""
        flow_id = _b64url_encode(secrets.token_bytes(16))
        with self._lock:
            self._prune(now)
            self._entries[flow_id] = {"challenge": challenge, "exp": now + self._ttl_seconds}
        return flow_id

    def consume(self, flow_id: Optional[str], now: float) -> Optional[bytes]:
        """Return and delete the challenge for a flow id, or None if missing/expired."""
        if not flow_id:
            return None
        with self._lock:
            entry = self._entries.pop(flow_id, None)
        if not entry:
            return None
        if now >= entry["exp"]:
            return None
        return entry["challenge"]

    def _prune(self, now: float) -> None:
        """Drop expired entries (called under lock)."""
        expired = [k for k, v in self._entries.items() if now >= v["exp"]]
        for key in expired:
            self._entries.pop(key, None)


class DashboardAuthStore:
    """
    Persistent store for dashboard passkeys plus session token signing.

    The backing JSON file holds the session secret and the list of registered
    credentials. The store is process-safe via an internal lock; concurrent
    requests mutate it through the provided methods only.
    """

    def __init__(
        self,
        path: str,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        """
        Initialise the store, loading or creating the backing file.

        Args:
            path: Path to the JSON file persisting auth state.
            session_ttl_seconds: Lifetime of issued session cookies in seconds.
        """
        self._path = Path(path).expanduser()
        self._session_ttl_seconds = session_ttl_seconds
        self._lock = threading.RLock()
        self._session_secret: bytes = b""
        self._credentials: List[Dict[str, Any]] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """Load state from disk, creating a fresh secret on first use."""
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                secret_b64 = data.get("session_secret", "")
                self._session_secret = _b64url_decode(secret_b64) if secret_b64 else b""
                creds = data.get("credentials", [])
                self._credentials = list(creds) if isinstance(creds, list) else []
            except (json.JSONDecodeError, ValueError, OSError) as exc:
                logger.error(f"Failed to load dashboard auth store {self._path}: {exc}")
                self._session_secret = b""
                self._credentials = []

        if not self._session_secret:
            self._session_secret = secrets.token_bytes(32)
            self._save()

    def _save(self) -> None:
        """Persist current state to disk atomically."""
        data = {
            "session_secret": _b64url_encode(self._session_secret),
            "credentials": self._credentials,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._path)

    # ------------------------------------------------------------------
    # Credential storage
    # ------------------------------------------------------------------
    def is_registered(self) -> bool:
        """Return True when at least one passkey is registered."""
        with self._lock:
            return len(self._credentials) > 0

    def may_register(self, has_valid_session: bool, has_valid_api_key: bool) -> bool:
        """
        Decide whether a passkey registration may proceed.

        Bootstrap (no passkeys yet) is allowed with the admin API key so the
        first passkey can be enrolled. Once any passkey exists, adding more
        requires an authenticated session — the API key alone must not be able
        to silently enroll a new authenticator.

        Args:
            has_valid_session: Whether the caller presented a valid session.
            has_valid_api_key: Whether the caller presented the admin API key.

        Returns:
            True when registration is permitted.
        """
        if self.is_registered():
            return has_valid_session
        return has_valid_session or has_valid_api_key

    def add_credential(
        self,
        credential_id: str,
        public_key: str,
        sign_count: int,
        transports: List[str],
        nickname: str,
    ) -> None:
        """
        Persist a newly registered passkey credential.

        Args:
            credential_id: base64url credential id (the WebAuthn raw id).
            public_key: base64url COSE public key bytes.
            sign_count: Initial authenticator signature counter.
            transports: Reported authenticator transports (e.g. ["internal"]).
            nickname: Human label for this passkey.
        """
        with self._lock:
            self._credentials = [c for c in self._credentials if c.get("id") != credential_id]
            self._credentials.append(
                {
                    "id": credential_id,
                    "public_key": public_key,
                    "sign_count": int(sign_count),
                    "transports": list(transports or []),
                    "nickname": nickname or "passkey",
                    "last_used": None,
                }
            )
            self._save()

    def get_credential(self, credential_id: str) -> Optional[Dict[str, Any]]:
        """Return the stored credential dict for an id, or None."""
        with self._lock:
            for cred in self._credentials:
                if cred.get("id") == credential_id:
                    return dict(cred)
        return None

    def list_credentials(self) -> List[Dict[str, Any]]:
        """Return display-safe credential metadata (never the public key)."""
        with self._lock:
            return [
                {
                    "id": c.get("id"),
                    "nickname": c.get("nickname"),
                    "transports": c.get("transports", []),
                    "last_used": c.get("last_used"),
                }
                for c in self._credentials
            ]

    def update_sign_count(self, credential_id: str, sign_count: int, last_used: Optional[float] = None) -> None:
        """Update the stored signature counter (and optional last_used) for a credential."""
        with self._lock:
            for cred in self._credentials:
                if cred.get("id") == credential_id:
                    cred["sign_count"] = int(sign_count)
                    if last_used is not None:
                        cred["last_used"] = last_used
                    self._save()
                    return

    def remove_credential(self, credential_id: str) -> bool:
        """
        Remove a passkey credential.

        Returns:
            True if a credential was removed, False if the id was unknown.
        """
        with self._lock:
            before = len(self._credentials)
            self._credentials = [c for c in self._credentials if c.get("id") != credential_id]
            removed = len(self._credentials) < before
            if removed:
                self._save()
            return removed

    def credential_ids(self) -> List[str]:
        """Return all registered credential ids (for allowCredentials)."""
        with self._lock:
            return [c.get("id") for c in self._credentials if c.get("id")]

    # ------------------------------------------------------------------
    # Session signing
    # ------------------------------------------------------------------
    def _sign(self, message: bytes) -> str:
        """Return the base64url HMAC-SHA256 of message under the session secret."""
        digest = hmac.new(self._session_secret, message, hashlib.sha256).digest()
        return _b64url_encode(digest)

    def issue_session(self, now: float) -> str:
        """
        Issue a signed session token valid for the configured TTL.

        Args:
            now: Current Unix timestamp (seconds).

        Returns:
            A ``<payload>.<signature>`` cookie value.
        """
        payload = {"sub": "admin", "exp": int(now) + self._session_ttl_seconds}
        payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = self._sign(payload_b64.encode("ascii"))
        return f"{payload_b64}.{signature}"

    def verify_session(self, token: Optional[str], now: float) -> bool:
        """
        Verify a session token's signature and expiry. Fails closed.

        Args:
            token: The cookie value to verify (may be malformed or None).
            now: Current Unix timestamp (seconds).

        Returns:
            True only when the token is well-formed, untampered, and unexpired.
        """
        if not token or not isinstance(token, str) or token.count(".") != 1:
            return False
        payload_b64, signature = token.split(".", 1)
        expected = self._sign(payload_b64.encode("ascii"))
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except (json.JSONDecodeError, ValueError):
            return False
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return now < exp
