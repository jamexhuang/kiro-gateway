# -*- coding: utf-8 -*-

"""
WebAuthn (FIDO2 passkey) ceremony service for the dashboard.

Thin orchestration over the ``webauthn`` library: it generates registration
and authentication options, manages the matching challenge through
``ChallengeStore``, verifies authenticator responses, and persists credential
state through ``DashboardAuthStore``. The route layer calls these functions and
maps results to HTTP responses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from loguru import logger
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from kiro.dashboard_auth import (
    ChallengeStore,
    DashboardAuthStore,
    _b64url_decode,
    _b64url_encode,
)

# Stable user handle for the single admin identity.
_ADMIN_USER_ID: bytes = b"kiro-dashboard-admin"
_ADMIN_USER_NAME: str = "admin"


class WebAuthnFlowError(Exception):
    """Raised when a ceremony cannot proceed (expired/unknown flow, bad input)."""


def begin_registration(
    store: DashboardAuthStore,
    challenges: ChallengeStore,
    rp_id: str,
    rp_name: str,
    origin: str,
    now: float,
) -> Tuple[str, Dict[str, Any]]:
    """
    Generate passkey registration options and stash the challenge.

    Args:
        store: Auth store (used to exclude already-registered credentials).
        challenges: Challenge store for the flow.
        rp_id: Relying Party id (the registrable domain).
        rp_name: Human-readable Relying Party name.
        origin: Expected origin (unused here, kept for signature symmetry).
        now: Current Unix timestamp.

    Returns:
        Tuple of (flow_id, options dict) to return to the browser.
    """
    exclude = [
        PublicKeyCredentialDescriptor(id=_b64url_decode(cid))
        for cid in store.credential_ids()
    ]
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=_ADMIN_USER_ID,
        user_name=_ADMIN_USER_NAME,
        user_display_name="Kiro Gateway Admin",
        exclude_credentials=exclude or None,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    flow_id = challenges.create(options.challenge, now)
    return flow_id, json.loads(options_to_json(options))


def complete_registration(
    store: DashboardAuthStore,
    challenges: ChallengeStore,
    rp_id: str,
    origin: str,
    flow_id: str,
    credential: Any,
    nickname: str,
    now: float,
) -> Dict[str, Any]:
    """
    Verify a registration response and persist the new credential.

    Raises:
        WebAuthnFlowError: When the flow is unknown/expired or verification fails.

    Returns:
        Display metadata for the stored credential.
    """
    challenge = challenges.consume(flow_id, now)
    if challenge is None:
        raise WebAuthnFlowError("Registration challenge expired or unknown")

    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
        )
    except Exception as exc:  # webauthn raises various verification errors
        logger.warning(f"Passkey registration verification failed: {exc}")
        raise WebAuthnFlowError("Passkey registration could not be verified") from exc

    credential_id = _b64url_encode(verification.credential_id)
    transports = _extract_transports(credential)
    store.add_credential(
        credential_id=credential_id,
        public_key=_b64url_encode(verification.credential_public_key),
        sign_count=verification.sign_count,
        transports=transports,
        nickname=nickname or "passkey",
    )
    return {"id": credential_id, "nickname": nickname or "passkey"}


def begin_authentication(
    store: DashboardAuthStore,
    challenges: ChallengeStore,
    rp_id: str,
    now: float,
) -> Tuple[str, Dict[str, Any]]:
    """
    Generate authentication (login) options and stash the challenge.

    Returns:
        Tuple of (flow_id, options dict).
    """
    allow = [
        PublicKeyCredentialDescriptor(id=_b64url_decode(cid))
        for cid in store.credential_ids()
    ]
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    flow_id = challenges.create(options.challenge, now)
    return flow_id, json.loads(options_to_json(options))


def complete_authentication(
    store: DashboardAuthStore,
    challenges: ChallengeStore,
    rp_id: str,
    origin: str,
    flow_id: str,
    credential: Any,
    now: float,
) -> bool:
    """
    Verify an authentication assertion. Fails closed.

    Returns:
        True when the assertion is valid for a registered credential.
    """
    challenge = challenges.consume(flow_id, now)
    if challenge is None:
        return False

    credential_id = _credential_id_of(credential)
    if not credential_id:
        return False
    stored = store.get_credential(credential_id)
    if not stored:
        return False

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=_b64url_decode(stored["public_key"]),
            credential_current_sign_count=int(stored.get("sign_count", 0)),
        )
    except Exception as exc:
        logger.warning(f"Passkey authentication verification failed: {exc}")
        return False

    store.update_sign_count(credential_id, verification.new_sign_count, last_used=now)
    return True


def _credential_id_of(credential: Any) -> str:
    """Extract the base64url credential id from an incoming credential payload."""
    if isinstance(credential, str):
        try:
            credential = json.loads(credential)
        except json.JSONDecodeError:
            return ""
    if isinstance(credential, dict):
        return str(credential.get("id") or credential.get("rawId") or "")
    return ""


def _extract_transports(credential: Any) -> List[str]:
    """Best-effort extraction of authenticator transports from a credential payload."""
    if isinstance(credential, str):
        try:
            credential = json.loads(credential)
        except json.JSONDecodeError:
            return []
    if isinstance(credential, dict):
        response = credential.get("response", {})
        if isinstance(response, dict):
            transports = response.get("transports")
            if isinstance(transports, list):
                return [str(t) for t in transports]
    return []
