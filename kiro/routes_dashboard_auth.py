# -*- coding: utf-8 -*-

"""
Dashboard passkey authentication routes and the unified authorization
dependency.

Authorization model:
- A valid session cookie (issued after a successful passkey login) OR the
  admin ``PROXY_API_KEY`` (for CLI/programmatic access) authorizes dashboard
  data and control endpoints.
- Passkey registration is gated: the API key bootstraps the first passkey;
  once any passkey exists, adding more requires an authenticated session.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel

from kiro import dashboard_auth_service as svc
from kiro.config import (
    DASHBOARD_AUTH_FILE,
    DASHBOARD_ORIGIN,
    DASHBOARD_RP_ID,
    DASHBOARD_RP_NAME,
    DASHBOARD_SESSION_TTL,
    PROXY_API_KEY,
)
from kiro.dashboard_auth import ChallengeStore, DashboardAuthStore

SESSION_COOKIE_NAME = "kiro_dash_session"

# Module-level singletons (tests swap these via monkeypatch).
dashboard_auth_store = DashboardAuthStore(
    path=DASHBOARD_AUTH_FILE, session_ttl_seconds=DASHBOARD_SESSION_TTL
)
dashboard_challenges = ChallengeStore(ttl_seconds=300)

auth_router = APIRouter()


def _origins() -> list:
    """Parse DASHBOARD_ORIGIN (comma-separated) into a list of expected origins."""
    return [o.strip() for o in DASHBOARD_ORIGIN.split(",") if o.strip()]


def _cookie_is_secure() -> bool:
    """Use a Secure cookie whenever any configured origin is https."""
    return any(o.lower().startswith("https://") for o in _origins())


def has_valid_session(request: Request) -> bool:
    """Return True when the request carries a valid dashboard session cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return dashboard_auth_store.verify_session(token, now=time.time())


def has_valid_api_key(request: Request) -> bool:
    """
    Return True when the request carries the admin API key.

    Accepts the key via the ``x-api-key`` header, an ``Authorization: Bearer``
    header, or the ``_auth`` query parameter (used by EventSource/SSE, which
    cannot set custom headers).
    """
    x_api_key = request.headers.get("x-api-key")
    if x_api_key and x_api_key == PROXY_API_KEY:
        return True
    authorization = request.headers.get("authorization")
    if authorization and authorization == f"Bearer {PROXY_API_KEY}":
        return True
    query_auth = request.query_params.get("_auth")
    return bool(query_auth and query_auth == PROXY_API_KEY)


def is_authenticated(request: Request) -> bool:
    """Return True when the request is authorized by session or API key."""
    return has_valid_session(request) or has_valid_api_key(request)


async def require_dashboard_auth(request: Request) -> bool:
    """
    FastAPI dependency: authorize via session cookie OR admin API key.

    Raises:
        HTTPException: 401 when neither a valid session nor API key is present.
    """
    if is_authenticated(request):
        return True
    raise HTTPException(status_code=401, detail="Dashboard authentication required")


async def optional_dashboard_auth(request: Request) -> bool:
    """Non-raising variant: returns whether the request is authenticated."""
    return is_authenticated(request)


def _set_session_cookie(response: Response) -> None:
    """Issue and attach a fresh session cookie."""
    token = dashboard_auth_store.issue_session(now=time.time())
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=DASHBOARD_SESSION_TTL,
        httponly=True,
        secure=_cookie_is_secure(),
        samesite="lax",
        path="/",
    )


# ----------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------
class RegisterCompleteRequest(BaseModel):
    """Body for completing passkey registration."""

    flow_id: str
    credential: Dict[str, Any]
    nickname: Optional[str] = None


class LoginCompleteRequest(BaseModel):
    """Body for completing passkey authentication."""

    flow_id: str
    credential: Dict[str, Any]


class AccountUpdateRequest(BaseModel):
    """Body for updating account enabled state or comment."""

    disabled: Optional[bool] = None
    comment: Optional[str] = None


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@auth_router.get("/dashboard/api/auth/status")
async def auth_status(request: Request) -> Dict[str, bool]:
    """Report whether a passkey is registered and whether the caller is authed."""
    return {
        "registered": dashboard_auth_store.is_registered(),
        "authenticated": is_authenticated(request),
    }


@auth_router.post("/dashboard/api/auth/register/begin")
async def register_begin(request: Request) -> Dict[str, Any]:
    """Begin passkey registration (gated by bootstrap/session rules)."""
    if not dashboard_auth_store.may_register(
        has_valid_session=has_valid_session(request),
        has_valid_api_key=has_valid_api_key(request),
    ):
        raise HTTPException(status_code=403, detail="Passkey registration not permitted")
    flow_id, options = svc.begin_registration(
        dashboard_auth_store,
        dashboard_challenges,
        rp_id=DASHBOARD_RP_ID,
        rp_name=DASHBOARD_RP_NAME,
        origin=_origins()[0] if _origins() else "",
        now=time.time(),
    )
    return {"flow_id": flow_id, "options": options}


@auth_router.post("/dashboard/api/auth/register/complete")
async def register_complete(request: Request, body: RegisterCompleteRequest) -> Dict[str, Any]:
    """Complete passkey registration and persist the credential."""
    if not dashboard_auth_store.may_register(
        has_valid_session=has_valid_session(request),
        has_valid_api_key=has_valid_api_key(request),
    ):
        raise HTTPException(status_code=403, detail="Passkey registration not permitted")
    try:
        result = svc.complete_registration(
            dashboard_auth_store,
            dashboard_challenges,
            rp_id=DASHBOARD_RP_ID,
            origin=_origins(),
            flow_id=body.flow_id,
            credential=body.credential,
            nickname=body.nickname or "passkey",
            now=time.time(),
        )
    except svc.WebAuthnFlowError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info(f"Dashboard passkey registered: {result.get('nickname')}")
    return {"registered": True, "passkey": result}


@auth_router.post("/dashboard/api/auth/login/begin")
async def login_begin() -> Dict[str, Any]:
    """Begin passkey authentication."""
    flow_id, options = svc.begin_authentication(
        dashboard_auth_store,
        dashboard_challenges,
        rp_id=DASHBOARD_RP_ID,
        now=time.time(),
    )
    return {"flow_id": flow_id, "options": options}


@auth_router.post("/dashboard/api/auth/login/complete")
async def login_complete(body: LoginCompleteRequest, response: Response) -> Dict[str, Any]:
    """Complete passkey authentication; set a session cookie on success."""
    ok = svc.complete_authentication(
        dashboard_auth_store,
        dashboard_challenges,
        rp_id=DASHBOARD_RP_ID,
        origin=_origins(),
        flow_id=body.flow_id,
        credential=body.credential,
        now=time.time(),
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Passkey authentication failed")
    _set_session_cookie(response)
    logger.info("Dashboard passkey login succeeded")
    return {"authenticated": True}


@auth_router.post("/dashboard/api/auth/logout")
async def logout(response: Response) -> Dict[str, bool]:
    """Clear the dashboard session cookie."""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"authenticated": False}


@auth_router.get("/dashboard/api/auth/passkeys")
async def list_passkeys(request: Request) -> Dict[str, Any]:
    """List registered passkeys (auth required)."""
    await require_dashboard_auth(request)
    return {"passkeys": dashboard_auth_store.list_credentials()}


@auth_router.delete("/dashboard/api/auth/passkeys/{credential_id}")
async def delete_passkey(credential_id: str, request: Request) -> Dict[str, bool]:
    """Remove a registered passkey (auth required)."""
    await require_dashboard_auth(request)
    removed = dashboard_auth_store.remove_credential(credential_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Passkey not found")
    return {"removed": True}


# ----------------------------------------------------------------------
# Accounts CRUD Routes
# ----------------------------------------------------------------------
@auth_router.get("/dashboard/api/accounts")
async def list_accounts(request: Request) -> Dict[str, Any]:
    """List all accounts (auth required)."""
    await require_dashboard_auth(request)
    account_manager = getattr(request.app.state, "account_manager", None)
    if not account_manager:
        return {"accounts": []}
    return {"accounts": account_manager.get_accounts_snapshot()}


@auth_router.post("/dashboard/api/accounts")
async def create_account(request: Request) -> Dict[str, Any]:
    """
    Add one or more accounts (auth required).

    Accepts a single credential dict ``{...}`` or a Kiro Cockpit-style
    array ``[{...}, ...]``.  When a list is provided each element is added
    individually and the response contains all created account IDs.
    """
    await require_dashboard_auth(request)
    account_manager = getattr(request.app.state, "account_manager", None)
    if not account_manager:
        raise HTTPException(status_code=500, detail="Account system not enabled")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Normalise: single dict → one-element list for uniform processing.
    entries: list
    if isinstance(body, list):
        entries = [e for e in body if isinstance(e, dict)]
        if not entries:
            raise HTTPException(
                status_code=400,
                detail="JSON array is empty or contains non-object elements",
            )
    elif isinstance(body, dict):
        entries = [body]
    else:
        raise HTTPException(
            status_code=400,
            detail="Body must be a JSON object or array of objects",
        )

    added_ids: list[str] = []
    errors: list[str] = []
    for idx, entry in enumerate(entries):
        try:
            account_id = await account_manager.add_account_entry(entry)
            added_ids.append(account_id)
        except ValueError as e:
            errors.append(f"#{idx + 1}: {e}")
        except Exception as e:
            logger.error(f"Failed to add account entry #{idx + 1}: {e}")
            errors.append(f"#{idx + 1}: {e}")

    if not added_ids and errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    result: Dict[str, Any] = {
        "success": True,
        "account_ids": added_ids,
        "account_id": added_ids[0] if added_ids else None,
        "added": len(added_ids),
    }
    if errors:
        result["partial_errors"] = errors
    return result


@auth_router.delete("/dashboard/api/accounts/{account_id:path}")
async def delete_account(account_id: str, request: Request) -> Dict[str, Any]:
    """Remove an account (auth required)."""
    await require_dashboard_auth(request)
    account_manager = getattr(request.app.state, "account_manager", None)
    if not account_manager:
        raise HTTPException(status_code=500, detail="Account system not enabled")
    
    success = await account_manager.remove_account_entry(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True}


@auth_router.patch("/dashboard/api/accounts/{account_id:path}")
async def update_account(
    account_id: str,
    request: Request,
    body: AccountUpdateRequest
) -> Dict[str, Any]:
    """Update an account enabled status or comment (auth required)."""
    await require_dashboard_auth(request)
    account_manager = getattr(request.app.state, "account_manager", None)
    if not account_manager:
        raise HTTPException(status_code=500, detail="Account system not enabled")
    
    success = await account_manager.update_account_entry(
        account_id, disabled=body.disabled, comment=body.comment
    )
    if not success:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True}

