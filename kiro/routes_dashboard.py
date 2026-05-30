# -*- coding: utf-8 -*-

"""
Web dashboard for runtime model routing and request monitoring.

The dashboard is intentionally self-contained: no asset build, no external CDN,
no startup hooks, and no background tasks.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import sys
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Security, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field

from kiro.config import PROXY_API_KEY, APP_VERSION
from kiro.control_panel import RoutingConfig, ThrottleConfig, control_panel
from kiro.routes_dashboard_auth import require_dashboard_auth, optional_dashboard_auth


router = APIRouter(tags=["Dashboard"])

dashboard_auth_header = APIKeyHeader(name="Authorization", auto_error=False)
dashboard_x_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


class RoutingUpdateRequest(BaseModel):
    """
    Partial runtime routing update.

    All fields are optional so the dashboard can update one setting at a time.
    """

    enabled: Optional[bool] = None
    mode: Optional[str] = None
    manual_model: Optional[str] = None
    redirects: Optional[Dict[str, str]] = None
    fallback_enabled: Optional[bool] = None
    fallback_models: Optional[list[str]] = None
    safe_fallback_to_original: Optional[bool] = None
    capture_content: Optional[bool] = None
    max_content_chars: Optional[int] = Field(default=None, ge=1000)


class RoutingPreviewRequest(BaseModel):
    """
    Request model preview for validating the active routing configuration.
    """

    model: str = Field(default="claude-opus-4-7", min_length=1)


class ThrottleUpdateRequest(BaseModel):
    """Partial burst protection update."""

    throttle_fast_fail: Optional[bool] = None
    enabled: Optional[bool] = None
    max_gap_ms: Optional[int] = Field(default=None, ge=500, le=30000)


async def verify_dashboard_api_key(request: Request) -> bool:
    """
    Authorize a dashboard request via passkey session OR admin API key.

    Delegates to the unified dependency in ``routes_dashboard_auth`` so all
    dashboard endpoints accept either a valid session cookie (passkey login) or
    the admin API key (CLI/programmatic).

    Raises:
        HTTPException: 401 when neither a valid session nor API key is present.
    """
    return await require_dashboard_auth(request)


async def optional_dashboard_api_key(request: Request) -> bool:
    """
    Non-raising authorization check (session OR API key).

    Keeps unauthenticated dashboard polling from producing 401 log noise while
    still withholding routing and monitoring data.
    """
    return await optional_dashboard_auth(request)




def _is_private_172(ip: str) -> bool:
    """Check if IP is in 172.16.0.0/12 (RFC 1918)."""
    if not ip.startswith("172."):
        return False
    parts = ip.split(".")
    if len(parts) < 2:
        return False
    try:
        return 16 <= int(parts[1]) <= 31
    except ValueError:
        return False


# Headers added by reverse proxies / tunnels (Cloudflare, nginx, etc.).
# Their presence proves the request did NOT arrive directly from the peer we
# observe in request.client.host, so network-position trust is unsafe.
_PROXY_FORWARDING_HEADERS: tuple = (
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
    "cf-connecting-ip",
    "cf-ray",
    "x-forwarded-host",
)


def _should_auto_inject_key(request: Request) -> bool:
    """
    Decide whether to auto-inject the proxy API key into the dashboard HTML.

    The key is injected ONLY for genuine direct loopback access (local
    development convenience). It is NEVER injected for requests that arrived
    through a reverse proxy or tunnel, because behind cloudflared/nginx the
    observed ``request.client.host`` is the proxy's own (often private) IP for
    every visitor — including the public internet. Trusting that IP would leak
    the admin key to anyone who opens the public dashboard URL.

    Args:
        request: The incoming HTTP request.

    Returns:
        True only when the request is a direct, non-proxied loopback request.
    """
    # Any forwarding header means a proxy/tunnel sits in front: never trust it.
    headers = request.headers
    for header_name in _PROXY_FORWARDING_HEADERS:
        if headers.get(header_name):
            return False

    client_host = request.client.host if request.client else ""
    return client_host in ("127.0.0.1", "::1", "localhost")

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> str:
    """
    Serve the dashboard shell.

    The HTML is public but it contains no data. All state-changing or sensitive
    API calls require the same proxy API key used by OpenAI/Anthropic endpoints.

    If accessed from localhost, the Proxy API Key is automatically injected
    to simplify local development and management.

    Returns:
        HTML dashboard.
    """
    # Auto-inject the admin key ONLY for direct loopback access (local dev).
    # Never for proxied/tunnelled requests — see _should_auto_inject_key.
    client_host = request.client.host if request.client else ""
    auto_inject = _should_auto_inject_key(request)

    if not auto_inject:
        logger.debug(f"Dashboard served without key injection: client={client_host}")

    html = DASHBOARD_HTML
    if auto_inject:
        # Inject the key into the HTML so the JS can pick it up
        # Escape quotes in key just in case
        escaped_key = PROXY_API_KEY.replace('"', '\\"')
        injection = f'<script>window.KIRO_AUTO_KEY = "{escaped_key}";</script>'
        # Try a few common head patterns
        if '<head>' in html:
            html = html.replace('<head>', f'<head>\n  {injection}')
        elif '<HEAD>' in html:
            html = html.replace('<HEAD>', f'<HEAD>\n  {injection}')
        else:
            # Fallback: just prepend to the whole thing
            html = injection + html

    # Inject version
    html = html.replace("APP_VERSION_PLACEHOLDER", APP_VERSION)

    return html


@router.get("/dashboard/api/state")
async def get_dashboard_state(
    request: Request,
    authenticated: bool = Security(optional_dashboard_api_key),
) -> Dict[str, Any]:
    """
    Return current runtime routing and monitoring state.

    Returns:
        Dashboard state snapshot.
    """
    if not authenticated:
        return {
            "authenticated": False,
            "routing": asdict(RoutingConfig()),
            "active_requests": [],
            "completed_requests": [],
            "accounts": [],
            "server_time": time.time(),
        }

    state = control_panel.snapshot()
    state["authenticated"] = True

    # Add account status if account_manager is available
    account_manager = getattr(request.app.state, "account_manager", None)
    if account_manager:
        state["accounts"] = account_manager.get_accounts_snapshot()
    else:
        state["accounts"] = []

    return state


@router.put("/dashboard/api/routing", dependencies=[Security(verify_dashboard_api_key)])
async def update_dashboard_routing(request_data: RoutingUpdateRequest) -> Dict[str, Any]:
    """
    Update runtime routing configuration.

    Args:
        request_data: Partial routing update.

    Returns:
        Updated routing configuration.

    Raises:
        HTTPException: 400 for invalid settings.
    """
    updates = request_data.model_dump(exclude_none=True)
    try:
        config = control_panel.update_routing_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"routing": config.__dict__}


@router.post("/dashboard/api/routing/reset", dependencies=[Security(verify_dashboard_api_key)])
async def reset_dashboard_routing() -> Dict[str, Any]:
    """
    Reset runtime routing to safe defaults.

    Returns:
        Reset routing configuration.
    """
    config = control_panel.reset_routing_config()
    return {"routing": config.__dict__}


@router.post("/dashboard/api/routing/test", dependencies=[Security(verify_dashboard_api_key)])
async def test_dashboard_routing(request_data: RoutingPreviewRequest) -> Dict[str, Any]:
    """
    Preview how the active routing configuration handles a model name.

    Args:
        request_data: Model name to route.

    Returns:
        Routing decision without mutating runtime state.
    """
    decision = control_panel.route_model(request_data.model.strip())
    return {"decision": decision.__dict__}


@router.get("/dashboard/api/throttle", dependencies=[Security(verify_dashboard_api_key)])
async def get_throttle_config() -> Dict[str, Any]:
    """Return current burst protection configuration."""
    from kiro.http_client import get_adaptive_gate
    result = asdict(control_panel.get_throttle_config())
    result["gate"] = get_adaptive_gate().snapshot()
    return {"throttle": result}


@router.put("/dashboard/api/throttle", dependencies=[Security(verify_dashboard_api_key)])
async def update_throttle_config(request_data: ThrottleUpdateRequest) -> Dict[str, Any]:
    """
    Update burst protection configuration.

    Changes take effect immediately — no restart needed.
    """
    updates = request_data.model_dump(exclude_none=True)

    # Separate gate-level settings from config-level settings
    max_gap_ms = updates.pop("max_gap_ms", None)

    if updates:
        try:
            config = control_panel.update_throttle_config(updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        config = control_panel.get_throttle_config()

    from kiro.http_client import update_burst_settings
    update_burst_settings(
        throttle_fast_fail=config.throttle_fast_fail,
        enabled=config.enabled,
        max_gap_ms=max_gap_ms,
    )

    # Return config + live gate state
    from kiro.http_client import get_adaptive_gate
    result = asdict(config)
    result["gate"] = get_adaptive_gate().snapshot()
    return {"throttle": result}


@router.post("/dashboard/api/monitor/clear", dependencies=[Security(verify_dashboard_api_key)])
async def clear_dashboard_monitor() -> Dict[str, str]:
    """
    Clear completed monitoring history.

    Returns:
        Operation status.
    """
    control_panel.clear_monitor()
    return {"status": "ok"}


@router.get("/dashboard/api/latency-tracing", dependencies=[Security(verify_dashboard_api_key)])
async def get_latency_tracing() -> Dict[str, bool]:
    """Return current latency tracing toggle state (runtime-mutable)."""
    import kiro.config
    return {"enabled": bool(kiro.config.LATENCY_TRACING_ENABLED)}


@router.put("/dashboard/api/latency-tracing", dependencies=[Security(verify_dashboard_api_key)])
async def set_latency_tracing(request: Request) -> Dict[str, bool]:
    """Toggle latency tracing at runtime without restart."""
    import kiro.config
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    enabled = bool(body.get("enabled", False))
    kiro.config.LATENCY_TRACING_ENABLED = enabled
    logger.info(f"Latency tracing toggled at runtime: {enabled}")
    return {"enabled": enabled}


@router.post("/dashboard/api/restart", dependencies=[Security(verify_dashboard_api_key)])
async def restart_gateway() -> Dict[str, str]:
    """
    Restart the gateway process to reload code.

    Uses os.execv to replace the current process with a fresh one.
    Active requests will be interrupted.
    """
    logger.warning("Gateway restart requested via dashboard")

    async def _do_restart():
        await asyncio.sleep(0.5)  # Let the response flush
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    asyncio.ensure_future(_do_restart())
    return {"status": "restarting"}


@router.get("/dashboard/api/metrics", dependencies=[Security(verify_dashboard_api_key)])
async def get_dashboard_metrics() -> Dict[str, Any]:
    """
    Return rolling-window request metrics for dashboard sparklines.

    Returns:
        Metrics snapshot with RPS, P50, P95, error count, and time series.
    """
    from kiro.metrics import metrics_registry
    return metrics_registry.snapshot(now=time.time())


@router.get("/dashboard/api/logs", dependencies=[Security(verify_dashboard_api_key)])
async def get_dashboard_logs(since: int = -1, limit: int = 500) -> Dict[str, Any]:
    """
    Return recent log entries from the in-memory ring buffer.

    Args:
        since: Return entries with seq > this value. -1 returns latest entries.
        limit: Max entries to return when since=-1.

    Returns:
        Log entries.
    """
    from kiro.log_buffer import log_buffer
    if since >= 0:
        entries = log_buffer.since(since)
    else:
        entries = log_buffer.snapshot(limit=limit)
    return {"entries": entries}


@router.get("/dashboard/api/models", dependencies=[Security(verify_dashboard_api_key)])
async def get_remote_models(request: Request, account_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch available models from Kiro remote API via an initialized account.

    Uses the gateway's own account_manager so credentials are handled per-account
    instead of relying on a single REFRESH_TOKEN env var.

    Args:
        account_id: Optional account ID. Falls back to the first initialized account.

    Returns:
        List of models with token limits, plus which account was used.
    """
    import httpx
    from kiro.auth import AuthType

    account_manager = getattr(request.app.state, "account_manager", None)
    if account_manager is None:
        return {"models": [], "error": "account_manager unavailable", "account_id": None}

    account = None
    if account_id:
        account = account_manager._accounts.get(account_id)
        if account is None:
            return {"models": [], "error": f"account not found: {account_id}", "account_id": None}
    else:
        for acct in account_manager._accounts.values():
            if acct.auth_manager is not None:
                account = acct
                break

    if account is None or account.auth_manager is None:
        return {"models": [], "error": "no initialized account available", "account_id": None}

    try:
        token = await account.auth_manager.get_access_token()
        url = f"{account.auth_manager.q_host}/ListAvailableModels"
        params: Dict[str, str] = {"origin": "AI_EDITOR"}
        if (
            account.auth_manager.auth_type == AuthType.KIRO_DESKTOP
            and account.auth_manager.profile_arn
        ):
            params["profileArn"] = account.auth_manager.profile_arn

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "models": data.get("models", []),
            "error": None,
            "account_id": account.id,
        }
    except httpx.HTTPStatusError as exc:
        return {
            "models": [],
            "error": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            "account_id": account.id,
        }
    except Exception as exc:
        return {"models": [], "error": str(exc), "account_id": account.id}


@router.get("/dashboard/api/events")
async def dashboard_events(
    request: Request,
    authenticated: bool = Security(verify_dashboard_api_key),
):
    """
    SSE stream of real-time dashboard events.

    Sends an initial snapshot, then incremental events as they occur.
    Sends keep-alive comments every 15s to prevent connection timeout.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    loop = asyncio.get_event_loop()

    def on_panel_event(evt):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, evt)
        except asyncio.QueueFull:
            pass

    def on_log_event(entry):
        try:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"event": "log", "data": entry}
            )
        except asyncio.QueueFull:
            pass

    from kiro.log_buffer import log_buffer
    control_panel.subscribe(on_panel_event)
    log_buffer.subscribe(on_log_event)

    account_manager = getattr(request.app.state, "account_manager", None)

    async def gen():
        from kiro.metrics import stage_metrics_registry
        from kiro.config import LATENCY_TRACING_ENABLED, LATENCY_SUMMARY_INTERVAL_S

        async def push_summaries():
            while True:
                try:
                    await asyncio.sleep(max(1.0, LATENCY_SUMMARY_INTERVAL_S))
                    if not LATENCY_TRACING_ENABLED:
                        continue
                    snap = stage_metrics_registry.snapshot(time.time())
                    queue.put_nowait({"event": "latency_summary", "data": snap})
                except asyncio.CancelledError:
                    raise
                except asyncio.QueueFull:
                    pass
                except Exception:
                    pass

        summary_task = asyncio.create_task(push_summaries())  # Always create; loop checks enabled flag
        try:
            try:
                snap = control_panel.snapshot()
                snap["accounts"] = (
                    account_manager.get_accounts_snapshot() if account_manager else []
                )
                yield f"event: snapshot\ndata: {_json.dumps(snap, default=str)}\n\n"
            except Exception as exc:
                logger.warning(f"SSE snapshot serialization failed: {exc}")
                yield f"event: snapshot\ndata: {{}}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {evt['event']}\ndata: {_json.dumps(evt['data'], default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                except Exception as exc:
                    logger.debug(f"SSE event serialization error: {exc}")
        finally:
            if summary_task is not None:
                summary_task.cancel()
            control_panel.unsubscribe(on_panel_event)
            log_buffer.unsubscribe(on_log_event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kiro Gateway 控制台</title>
  <style>
    :root{--ink:#18211f;--muted:#65706c;--paper:#f5f1e8;--card:rgba(255,253,247,.9);
      --line:rgba(24,33,31,.13);--accent:#c45f2c;--accent-2:#1d6f68;--danger:#9b2d20;
      --shadow:0 12px 30px rgba(43,35,24,.12)}
    *{box-sizing:border-box}html,body{margin:0;height:100%}
    body{color:var(--ink);background:var(--paper);
      font:13px/1.45 "Inter","IBM Plex Sans","Helvetica Neue",sans-serif}
    .app{display:grid;grid-template-columns:200px 1fr;min-height:100vh}
    nav.sidebar{background:#efe9dc;border-right:1px solid var(--line);padding:14px 12px;
      position:sticky;top:0;height:100vh;overflow:auto}
    nav h1{font:700 14px/1.2 "Iowan Old Style",Georgia,serif;margin:0 0 18px;letter-spacing:-.01em}
    nav .ver{color:var(--muted);font-size:11px;margin-bottom:14px}
    nav a{display:block;padding:7px 9px;border-radius:8px;color:var(--ink);text-decoration:none;
      font-weight:600;font-size:12.5px;margin-bottom:2px;cursor:pointer}
    nav a:hover,nav a.active{background:rgba(24,33,31,.08)}
    nav .key{margin-top:20px}nav .key label{display:block;font-size:10px;color:var(--muted);margin-bottom:4px;
      text-transform:uppercase;letter-spacing:.08em}
    nav input[type=password]{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;
      background:#fff;font:12px "SF Mono",Menlo,monospace}
    nav .status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent-2);
      margin-right:6px;vertical-align:middle}
    nav .status-dot.err{background:var(--danger)}
    nav .status-dot.off{background:#aaa}
    main{padding:14px 18px 40px;overflow-x:hidden}
    .stripe{display:grid;grid-template-columns:repeat(5,1fr) 2fr;gap:10px;margin-bottom:14px;align-items:stretch}
    .stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
    .stat .lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
    .stat .val{font:700 18px/1.1 "SF Mono",Menlo,monospace;margin-top:2px}
    .spark{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 10px;display:flex;
      align-items:center;gap:8px}
    .spark svg{flex:1;height:36px}
    section.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;
      margin-bottom:14px;scroll-margin-top:12px}
    section.panel>header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px}
    section.panel h2{margin:0;font:600 13px/1 "Inter",sans-serif;letter-spacing:.02em;text-transform:uppercase;color:var(--muted)}
    .btn{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 10px;font:600 12px/1 inherit;
      cursor:pointer;color:var(--ink)}
    .btn.primary{background:var(--ink);color:#fffaf0;border-color:var(--ink)}
    .btn.warn{background:var(--danger);color:#fff;border-color:var(--danger)}
    .btn.ghost{background:transparent}
    .btn.warn.ghost{color:var(--danger);border-color:transparent}
    .btn.warn.ghost:hover{background:rgba(155,45,32,.08)}
    .btn+.btn{margin-left:6px}
    .accounts{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
    .acct{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#fffcf3;font-size:12px}
    .acct.current{border-color:var(--accent-2);background:rgba(29,111,104,.06)}
    .acct .name{font-weight:700;display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
    .tag{font-size:10px;padding:1px 6px;border-radius:999px;background:var(--line);color:var(--muted);
      text-transform:uppercase;letter-spacing:.08em}
    .tag.on{background:var(--accent-2);color:#fff}
    .tag.err{background:var(--danger);color:#fff}
    .tag.dis{background:#aaa;color:#fff}
    .acct dl{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin:0;font-size:11px}
    .acct dt{color:var(--muted)}.acct dd{margin:0;font-weight:700}
    .acct .cooldown{margin-top:6px;font-size:11px;color:var(--danger)}
    table.reqs{width:100%;border-collapse:collapse;font-size:12px;font-family:"SF Mono",Menlo,monospace}
    table.reqs th{text-align:left;color:var(--muted);font-weight:600;padding:4px 6px;font-size:10.5px;
      text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--line)}
    table.reqs td{padding:4px 6px;border-bottom:1px solid rgba(24,33,31,.06);vertical-align:top}
    table.reqs tr.row{cursor:pointer}table.reqs tr.row:hover{background:rgba(24,33,31,.04)}
    table.reqs tr.exp td{background:#fffbe9}
    .statusbadge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10.5px;font-weight:700}
    .statusbadge.ok{background:rgba(29,111,104,.15);color:var(--accent-2)}
    .statusbadge.err{background:rgba(155,45,32,.15);color:var(--danger)}
    .statusbadge.active{background:rgba(196,95,44,.15);color:var(--accent)}
    .statusbadge.dis{background:rgba(24,33,31,.08);color:var(--muted)}
    details.payload{margin:4px 0;font-family:"SF Mono",Menlo,monospace}
    details.payload>summary{cursor:pointer;color:var(--accent);font-weight:700;font-size:11px}
    pre.code{white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;background:#18211f;
      color:#fff4dd;border-radius:6px;padding:8px;font:11px/1.45 "SF Mono",Menlo,monospace;margin:6px 0 0}
    .log-console{background:#18211f;color:#e5e0d1;border-radius:8px;padding:0;overflow:hidden;
      font:11.5px/1.5 "SF Mono",Menlo,monospace;display:flex;flex-direction:column;height:340px}
    .log-console header{background:#21302d;display:flex;gap:6px;padding:6px 8px;align-items:center;flex-wrap:wrap}
    .log-console input.filter{flex:1;min-width:160px;background:#0f1918;color:#e5e0d1;border:1px solid #334;
      border-radius:4px;padding:3px 6px;font:inherit}
    .log-console select,.log-console label{color:#e5e0d1;font-size:11px}
    .log-console .body{flex:1;overflow:auto;padding:6px 10px}
    .log-console .line{white-space:pre-wrap;word-break:break-word;padding:1px 0}
    .log-console .lvl-WARNING{color:#f7c06f}.log-console .lvl-ERROR{color:#ff8d7a}
    .log-console .lvl-SUCCESS{color:#7ecf9a}.log-console .lvl-INFO{color:#cfd3c8}
    .log-console .lvl-DEBUG{color:#8892a0}
    .toolbar{display:flex;gap:6px;align-items:center}
    .search{padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:#fff;font:12px inherit;width:220px}
    .filter-chip{background:#fff;border:1px solid var(--line);border-radius:999px;padding:2px 8px;font-size:11px;color:var(--muted);cursor:pointer}
    .filter-chip.on{background:var(--ink);color:#fffaf0;border-color:var(--ink)}
    .hidden{display:none!important}
    
    /* Auth overlay styles */
    .auth-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:var(--paper);z-index:9999;
      display:flex;align-items:center;justify-content:center}
    .auth-card{width:380px;background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:24px;box-shadow:var(--shadow);text-align:center}
    .auth-card h2{margin:0 0 12px;font:700 16px/1.2 "Iowan Old Style",Georgia,serif;letter-spacing:-.01em}
    .auth-card .input-group{margin-bottom:12px;text-align:left}
    .auth-card .input-group label{display:block;font-size:10px;color:var(--muted);margin-bottom:4px;
      text-transform:uppercase;letter-spacing:.08em}
    .auth-card input{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;
      background:#fff;font-size:12px}

    /* Mobile responsive */
    @media (max-width: 960px){
      .app{grid-template-columns:1fr}
      nav.sidebar{position:fixed;bottom:0;left:0;right:0;top:auto;height:auto;z-index:100;
        border-right:none;border-top:1px solid var(--line);padding:8px 12px;
        display:flex;flex-wrap:wrap;gap:4px;align-items:center;overflow-x:auto}
      nav h1{margin:0 8px 0 0;font-size:13px}
      nav .ver{display:none}
      nav .key{display:none}
      nav a{padding:5px 8px;font-size:11.5px;margin:0;white-space:nowrap}
      main{padding:10px 10px 70px;overflow-x:auto}
      .stripe{grid-template-columns:repeat(3,1fr);gap:6px}
      .spark{display:none}
      .stat .val{font-size:14px}
      section.panel{padding:10px}
      section.panel>header{flex-wrap:wrap}
      .toolbar{flex-wrap:wrap}
      .search{width:100%;min-width:0}
      table.reqs{font-size:11px;display:block;overflow-x:auto;white-space:nowrap}
      table.reqs th,table.reqs td{padding:3px 4px}
      .accounts{grid-template-columns:1fr}
      .log-console{height:240px}
      pre.code{font-size:10px;max-height:180px}
    }
    @media (max-width: 480px){
      .stripe{grid-template-columns:1fr 1fr}
      section.panel h2{font-size:11.5px}
      .btn{padding:3px 7px;font-size:11px}
    }
  </style>
</head>
<body>

<!-- Passkey Secure Authentication Screen -->
<div id="auth-overlay" class="auth-overlay hidden">
  <div class="auth-card">
    <h2 id="auth-title">Kiro Gateway 安全驗證</h2>
    
    <div id="register-form" class="hidden">
      <p style="font-size:11px;color:var(--muted);margin-bottom:12px;text-align:left">系統尚未設定任何通行密鑰，請輸入目前的代理 API 金鑰以註冊第一個管理員通行密鑰（FIDO2 Passkey）：</p>
      <div class="input-group">
        <label>密鑰名稱 (例如：個人筆電)</label>
        <input id="reg-nickname" type="text" value="主管理員密鑰">
      </div>
      <div class="input-group">
        <label>代理 API 金鑰 (PROXY_API_KEY)</label>
        <input id="reg-bootstrap-key" type="password" placeholder="輸入 PROXY_API_KEY 進行 Bootstrap">
      </div>
      <button class="btn primary" style="width:100%;padding:8px;margin-top:6px" onclick="doRegister()">註冊通行密鑰</button>
    </div>
    
    <div id="login-form" class="hidden">
      <p style="font-size:11.5px;color:var(--muted);margin-bottom:18px">請使用您在此裝置或安全金鑰中註冊的通行密鑰登入控制台。</p>
      <button class="btn primary" style="width:100%;padding:10px;font-size:13px" onclick="doLogin()">使用通行密鑰安全登入</button>
    </div>
    
    <div id="auth-error" style="color:var(--danger);font-size:11px;margin-top:12px;text-align:center;word-break:break-all"></div>
  </div>
</div>

<div class="app">
  <nav class="sidebar">
    <h1>Kiro Gateway</h1>
    <div class="ver">vAPP_VERSION_PLACEHOLDER · <span id="connStatus"><span class="status-dot off"></span>未連線</span></div>
    <a data-jump="panel-status" class="active">總覽</a>
    <a data-jump="panel-latency">延遲分析</a>
    <a data-jump="panel-routing">路由設定</a>
    <a data-jump="panel-throttle">流量控制</a>
    <a data-jump="panel-accounts">帳號</a>
    <a data-jump="panel-passkeys">通行密鑰</a>
    <a data-jump="panel-requests">請求總覽</a>
    <a data-jump="panel-logs">即時日誌</a>
    <a data-jump="panel-models">遠端模型</a>
    <div class="key" id="sidebar-auth-area">
      <!-- Session managed dynamically by checkAuth -->
      <label>代理 API 金鑰</label>
      <input id="apiKey" type="password" placeholder="輸入 Bearer 金鑰">
      <div style="margin-top:6px"><button class="btn primary" onclick="saveKey()">連線</button><button class="btn ghost" onclick="forgetKey()">清除</button></div>
      <div style="margin-top:10px"><button class="btn warn" onclick="restartGateway()">重啟 Gateway</button></div>
    </div>
  </nav>
  <main>
    <section id="panel-status" class="panel">
      <header><h2>總覽（近 5 分鐘）</h2><div id="stripeMeta" class="muted" style="font-size:11px;color:var(--muted)"></div></header>
      <div class="stripe">
        <div class="stat"><div class="lbl">每秒請求</div><div class="val" id="mRps">—</div></div>
        <div class="stat"><div class="lbl">P50 延遲</div><div class="val" id="mP50">—</div></div>
        <div class="stat"><div class="lbl">P95 延遲</div><div class="val" id="mP95">—</div></div>
        <div class="stat"><div class="lbl">錯誤率</div><div class="val" id="mErr">—</div></div>
        <div class="stat"><div class="lbl">進行中</div><div class="val" id="mActive">—</div></div>
        <div class="spark"><div class="lbl">RPS</div><svg id="sparkRps"></svg><div class="lbl">ERR</div><svg id="sparkErr"></svg></div>
      </div>
      <p style="font-size:11px;color:var(--muted);margin-top:8px">滾動窗口統計近 5 分鐘的請求量、延遲百分位數和錯誤率。火花圖顯示每 5 秒一個桶的趨勢。</p>
    </section>

    <section id="panel-routing" class="panel">
      <header><h2>路由與快取設定</h2><button class="btn warn ghost" onclick="resetRouting()">重設預設</button></header>
      <div style="display:flex;gap:12px;margin-bottom:8px">
        <label class="chip"><input id="enabled" type="checkbox"> 啟用自訂路由</label>
        <label class="chip"><input id="fallbackEnabled" type="checkbox"> 啟用故障自動降級</label>
        <label class="chip"><input id="safeFallback" type="checkbox"> 降級時保留原始內容</label>
        <label class="chip"><input id="captureContent" type="checkbox"> 擷取請求內容</label>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px">
        <div><label style="font-size:10.5px;color:var(--muted)">模式</label>
          <select id="mode" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"><option value="passthrough">直通</option><option value="manual">手動</option><option value="redirect">重導向</option></select></div>
        <div><label style="font-size:10.5px;color:var(--muted)">手動指定模型</label>
          <select id="manualModel" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"></select></div>
        <div><label style="font-size:10.5px;color:var(--muted)">降級模型（逗號分隔）</label>
          <input id="fallbackModels" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"></div>
      </div>
      <label style="font-size:10.5px;color:var(--muted);display:block;margin-top:8px">重導向規則 JSON</label>
      <textarea id="redirects" style="width:100%;min-height:64px;padding:6px;border:1px solid var(--line);border-radius:6px;font:12px 'SF Mono',Menlo,monospace"></textarea>
      
      <!-- Routing Simulator Preview Panel -->
      <div id="routing-preview-wrap" class="hidden" style="margin-top:10px;padding:8px;border:1px solid var(--line);border-radius:6px;background:rgba(24,33,31,.02)">
        <h3 style="margin:0 0 6px;font-size:11px;text-transform:uppercase;color:var(--muted)">預估路由效果 (Preview)</h3>
        <div id="routing-preview-results" style="font-family:'SF Mono',Menlo,monospace;font-size:11.5px"></div>
      </div>

      <div style="margin-top:8px">
        <button class="btn primary" onclick="applyRouting()">套用</button>
        <button class="btn ghost" onclick="previewRouting()">測試預覽</button>
        <span id="saveResult" style="margin-left:10px;color:var(--muted)"></span>
      </div>
    </section>

    <section id="panel-throttle" class="panel">
      <header><h2>流量控制</h2>
        <div class="toolbar">
          <button class="btn primary" onclick="applyThrottle()">套用</button>
          <span id="throttleResult" style="font-size:11px;color:var(--muted)"></span>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">控制上游請求並發數和啟動抖動，防止 subagent 團隊同時發送請求觸發 429。調整後即時生效，無需重啟。</p>
      <div style="display:grid;grid-template-columns:repeat(3,1fr) auto;gap:8px;align-items:end">
        <div><label style="font-size:10.5px;color:var(--muted)">目前間隔 (ms)</label>
          <div id="gateGap" style="font:700 16px 'SF Mono',Menlo,monospace;padding:5px 0">—</div></div>
        <div><label style="font-size:10.5px;color:var(--muted)">連續成功</label>
          <div id="gateSuccesses" style="font:700 16px 'SF Mono',Menlo,monospace;padding:5px 0">—</div></div>
        <div><label style="font-size:10.5px;color:var(--muted)">連續 429</label>
          <div id="gate429s" style="font:700 16px 'SF Mono',Menlo,monospace;padding:5px 0;color:var(--danger)">—</div></div>
        <label class="chip"><input id="burstEnabled" type="checkbox" checked> 啟用</label>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px;align-items:end">
        <div><label style="font-size:10.5px;color:var(--muted)">最大間隔 (ms)</label>
          <input id="maxGapMs" type="number" min="500" max="30000" value="8000" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"></div>
        <div><label style="font-size:10.5px;color:var(--muted)">THROTTLING 策略</label>
          <select id="throttleStrategy" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"><option value="retry">重試 + 自適應</option><option value="fastfail">快速失敗 → 換帳號</option></select></div>
        <div><button class="btn primary" onclick="applyThrottle()">套用</button><span id="throttleResult" style="margin-left:8px;font-size:11px;color:var(--muted)"></span></div>
      </div>
    </section>

    <section id="panel-accounts" class="panel">
      <header>
        <h2>帳號 <span id="accountCount" class="tag">0</span></h2>
        <button class="btn primary" onclick="showAddAccountModal()">新增帳號</button>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">顯示所有 API 帳號的即時狀態。「使用中」為當前輪轉到的帳號；「冷卻中」表示該帳號因錯誤觸發指數退避，倒數結束後自動恢復。</p>
      
      <!-- Add Account Section -->
      <div id="add-account-wrap" class="hidden" style="margin-bottom:12px;padding:12px;border:1px solid var(--line);border-radius:8px;background:rgba(24,33,31,.02)">
        <h3 style="margin:0 0 8px;font-size:12px;text-transform:uppercase;color:var(--muted)">新增 Kiro 帳號憑證</h3>
        <div style="display:grid;grid-template-columns:1fr 2fr auto;gap:8px;align-items:end">
          <div>
            <label style="font-size:10px;color:var(--muted);display:block;margin-bottom:4px">憑證類型</label>
            <select id="new-acct-type" onchange="toggleNewAcctType()" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px">
              <option value="json">JSON 檔案 / Token 內容</option>
              <option value="refresh_token">Refresh Token 直填</option>
            </select>
          </div>
          <div id="new-acct-token-wrap">
            <label style="font-size:10px;color:var(--muted);display:block;margin-bottom:4px">憑證 JSON 內容 (支援 Kiro Cockpit/IDE 格式)</label>
            <input id="new-acct-token" type="text" placeholder='貼上完整 JSON 例如 {"refreshToken": "..."}' style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px">
          </div>
          <div id="new-acct-rt-wrap" class="hidden" style="display:none">
            <label style="font-size:10px;color:var(--muted);display:block;margin-bottom:4px">Refresh Token</label>
            <input id="new-acct-rt" type="text" placeholder="貼上 128 位元 Refresh Token" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px">
          </div>
          <div>
            <label style="font-size:10px;color:var(--muted);display:block;margin-bottom:4px">備註暱稱 (Email / 用途)</label>
            <input id="new-acct-comment" type="text" placeholder="例如: user@gmail.com" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px">
          </div>
        </div>
        <div style="margin-top:10px">
          <button class="btn primary" onclick="submitAddAccount()">提交新增</button>
          <button class="btn ghost" onclick="hideAddAccountModal()">取消</button>
          <span id="addAcctError" style="margin-left:8px;color:var(--danger);font-size:11px"></span>
        </div>
      </div>

      <div id="accounts" class="accounts"><p style="color:var(--muted)">等待資料…</p></div>
    </section>

    <section id="panel-passkeys" class="panel">
      <header>
        <h2>通行密鑰 (Passkeys)</h2>
        <button class="btn primary" onclick="addNewPasskey()">新增通行密鑰</button>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">此裝置或其他安全密鑰的 FIDO2 / WebAuthn 憑證。註冊新密鑰時，將提示瀏覽器新增本機或外部金鑰。</p>
      <div id="passkeysWrap" style="max-height:240px;overflow:auto"><p style="color:var(--muted)">載入中…</p></div>
    </section>

    <section id="panel-latency" class="panel">
      <header><h2>延遲分析（每段 P50/P95）</h2>
        <div class="toolbar">
          <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;cursor:pointer"><input id="latencyToggle" type="checkbox"> 啟用追蹤</label>
          <span id="latencyMeta" class="tag" style="font-size:11px;color:var(--muted)">未啟用</span>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">每段請求延遲的滾動窗口分位數（單位 ms）。勾選「啟用追蹤」即時開關，無需重啟。<br>
        <strong>auth</strong> = 取 token / <strong>gate_wait</strong> = burst 節流等候 / <strong>upstream_connect</strong> = 上游連線握手 / <strong>ttft</strong> = 首 token / <strong>streaming</strong> = 首 token 後串流耗時。</p>
      <div id="latencyTable"><p style="color:var(--muted)">等待資料…</p></div>
    </section>

    <section id="panel-requests" class="panel">
      <header><h2>請求總覽（進行中 + 歷史）</h2>
        <div class="toolbar">
          <span id="activeCount" class="tag">0 進行中</span>
          <input id="reqFilter" class="search" placeholder="篩選：模型 / 狀態 / 帳號 / 錯誤">
          <button class="btn ghost" onclick="clearMonitor()">清除歷史</button>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">進行中與最近 200 筆已完成請求合併在同一表中，依時間倒序。狀態徽章：<span class="statusbadge active">active</span> = 串流中、<span class="statusbadge ok">completed</span> = 已完成、<span class="statusbadge dis">client_disconnected</span> = 客戶端斷線、<span class="statusbadge err">error</span> = 失敗。點擊列展開查看 latency trace、嘗試明細、payload。</p>
      <div id="requestsWrap" style="max-height:680px;overflow:auto"><p style="color:var(--muted)">尚無請求。</p></div>
    </section>

    <section id="panel-logs" class="panel">
      <header><h2>即時日誌</h2>
        <div class="toolbar">
          <span class="filter-chip on" data-level="ALL">全部</span>
          <span class="filter-chip" data-level="WARNING">警告以上</span>
          <span class="filter-chip" data-level="ERROR">僅錯誤</span>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">透過 SSE 即時串流伺服器日誌（最多保留 2000 筆）。可依等級篩選或用關鍵字搜尋。日誌包含請求路由決策、帳號切換、錯誤堆疊等資訊。</p>
      <div class="log-console">
        <header>
          <input id="logSearch" class="filter" placeholder="搜尋日誌關鍵字…">
          <label><input id="logAutoScroll" type="checkbox" checked> 自動滾動</label>
          <button class="btn ghost" style="color:#e5e0d1;border-color:#334" onclick="clearLogView()">清空畫面</button>
        </header>
        <div id="logBody" class="body"></div>
      </div>
    </section>

    <section id="panel-models" class="panel">
      <header><h2>遠端模型</h2>
        <div class="toolbar">
          <button class="btn primary" onclick="loadModels()">重新載入</button>
          <span id="modelsStatus" style="font-size:11px;color:var(--muted)"></span>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">查詢 Kiro 遠端 API 目前可用的模型清單，包含各模型的輸入/輸出 token 上限。可用於確認帳號權限 and 模型可用性。</p>
      <div id="modelsWrap"><p style="color:var(--muted)">點擊「重新載入」查詢遠端模型。</p></div>
    </section>
  </main>
</div>

<script>
const $=s=>document.querySelector(s);const $$=s=>document.querySelectorAll(s);
const autoKey=window.KIRO_AUTO_KEY;let storedKey=localStorage.getItem("kiro-dashboard-key");
if(autoKey&&(!storedKey||storedKey.length<3)){localStorage.setItem("kiro-dashboard-key",autoKey);storedKey=autoKey;}
if($("#apiKey")) $("#apiKey").value=storedKey||"";

// -------- WebAuthn / Passkey JS Helpers --------
function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let string = '';
  for (let i = 0; i < bytes.byteLength; i++) {
    string += String.fromCharCode(bytes[i]);
  }
  const base64 = btoa(string);
  return base64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}

function base64urlToBuffer(base64url) {
  let base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
  while (base64.length % 4) {
    base64 += '=';
  }
  const string = atob(base64);
  const buffer = new ArrayBuffer(string.length);
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < string.length; i++) {
    bytes[i] = string.charCodeAt(i);
  }
  return buffer;
}

function prepareRegisterOptions(options) {
  options.challenge = base64urlToBuffer(options.challenge);
  options.user.id = base64urlToBuffer(options.user.id);
  if (options.excludeCredentials) {
    options.excludeCredentials.forEach(cred => {
      cred.id = base64urlToBuffer(cred.id);
    });
  }
  return { publicKey: options };
}

function prepareLoginOptions(options) {
  options.challenge = base64urlToBuffer(options.challenge);
  if (options.allowCredentials) {
    options.allowCredentials.forEach(cred => {
      cred.id = base64urlToBuffer(cred.id);
    });
  }
  return { publicKey: options };
}

function credentialToJSON(cred) {
  const response = {};
  if (cred.response.clientDataJSON) {
    response.clientDataJSON = bufferToBase64url(cred.response.clientDataJSON);
  }
  if (cred.response.attestationObject) {
    response.attestationObject = bufferToBase64url(cred.response.attestationObject);
  }
  if (cred.response.authenticatorData) {
    response.authenticatorData = bufferToBase64url(cred.response.authenticatorData);
  }
  if (cred.response.signature) {
    response.signature = bufferToBase64url(cred.response.signature);
  }
  if (cred.response.userHandle) {
    response.userHandle = bufferToBase64url(cred.response.userHandle);
  }
  
  const transports = typeof cred.response.getTransports === 'function' ? cred.response.getTransports() : [];
  
  return {
    id: cred.id,
    rawId: bufferToBase64url(cred.rawId),
    type: cred.type,
    response: response,
    clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
    transports: transports
  };
}

let authState = { registered: false, authenticated: false };

async function checkAuth() {
  try {
    const res = await fetch("/dashboard/api/auth/status");
    if (!res.ok) throw new Error(await res.text());
    const d = await res.json();
    authState.registered = d.registered;
    authState.authenticated = d.authenticated;
    
    if (d.authenticated) {
      $("#auth-overlay").classList.add("hidden");
      connect();
    } else {
      $("#auth-overlay").classList.remove("hidden");
      $("#auth-error").textContent = "";
      if (!d.registered) {
        $("#register-form").classList.remove("hidden");
        $("#login-form").classList.add("hidden");
      } else {
        $("#register-form").classList.add("hidden");
        $("#login-form").classList.remove("hidden");
        doLogin();
      }
    }
  } catch (e) {
    $("#auth-overlay").classList.remove("hidden");
    $("#auth-error").textContent = "無法取得認證狀態: " + e.message;
  }
}

async function doRegister() {
  const nickname = $("#reg-nickname").value.trim() || "主管理員密鑰";
  const bootstrapKey = $("#reg-bootstrap-key").value.trim();
  $("#auth-error").textContent = "";
  
  if (!bootstrapKey && !storedKey) {
    $("#auth-error").textContent = "請輸入代理 API 金鑰 (PROXY_API_KEY)";
    return;
  }
  
  const authKey = bootstrapKey || storedKey;
  
  try {
    const beginRes = await fetch("/dashboard/api/auth/register/begin", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${authKey}`
      }
    });
    if (beginRes.status === 401 || beginRes.status === 403) {
      throw new Error("金鑰錯誤，無法驗證");
    }
    if (!beginRes.ok) throw new Error(await beginRes.text());
    const beginData = await beginRes.json();
    const flowId = beginData.flow_id;
    
    const options = prepareRegisterOptions(beginData.options);
    const credential = await navigator.credentials.create(options);
    const credentialJSON = credentialToJSON(credential);
    
    const completeRes = await fetch("/dashboard/api/auth/register/complete", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${authKey}`
      },
      body: JSON.stringify({
        flow_id: flowId,
        credential: credentialJSON,
        nickname: nickname
      })
    });
    
    if (!completeRes.ok) throw new Error(await completeRes.text());
    
    localStorage.setItem("kiro-dashboard-key", authKey);
    if ($("#apiKey")) $("#apiKey").value = authKey;
    
    $("#auth-error").textContent = "註冊成功，正在進行通行密鑰登入...";
    setTimeout(doLogin, 500);
  } catch (e) {
    $("#auth-error").textContent = "註冊失敗: " + e.message;
  }
}

async function doLogin() {
  $("#auth-error").textContent = "";
  try {
    const beginRes = await fetch("/dashboard/api/auth/login/begin", { method: "POST" });
    if (!beginRes.ok) throw new Error(await beginRes.text());
    const beginData = await beginRes.json();
    const flowId = beginData.flow_id;
    
    const options = prepareLoginOptions(beginData.options);
    const credential = await navigator.credentials.get(options);
    const credentialJSON = credentialToJSON(credential);
    
    const completeRes = await fetch("/dashboard/api/auth/login/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        flow_id: flowId,
        credential: credentialJSON
      })
    });
    
    if (!completeRes.ok) throw new Error(await completeRes.text());
    
    checkAuth();
  } catch (e) {
    if (e.name === "NotAllowedError") {
      $("#auth-error").textContent = "使用者取消了通行密鑰驗證。";
    } else {
      $("#auth-error").textContent = "登入失敗: " + e.message;
    }
  }
}

async function addNewPasskey() {
  const nickname = prompt("請輸入新通行密鑰的暱稱 (例如：手機、備用金鑰):");
  if (!nickname) return;
  try {
    const beginData = await api("/dashboard/api/auth/register/begin", { method: "POST" });
    const flowId = beginData.flow_id;
    const options = prepareRegisterOptions(beginData.options);
    const credential = await navigator.credentials.create(options);
    const credentialJSON = credentialToJSON(credential);
    
    await api("/dashboard/api/auth/register/complete", {
      method: "POST",
      body: JSON.stringify({
        flow_id: flowId,
        credential: credentialJSON,
        nickname: nickname
      })
    });
    
    alert("成功新增通行密鑰！");
    loadPasskeys();
  } catch (e) {
    alert("新增通行密鑰失敗: " + e.message);
  }
}

async function loadPasskeys() {
  const wrap = $("#passkeysWrap");
  if (!wrap) return;
  try {
    const d = await api("/dashboard/api/auth/passkeys");
    const passkeys = d.passkeys || [];
    if (!passkeys.length) {
      wrap.innerHTML = `<p style="color:var(--muted)">尚未設定任何通行密鑰。</p>`;
      return;
    }
    let html = `<table class="reqs" style="width:100%"><thead><tr><th>暱稱</th><th>註冊時間</th><th>最後使用</th><th>Credential ID</th><th>操作</th></tr></thead><tbody>`;
    passkeys.forEach(p => {
      const created = p.created_at ? new Date(p.created_at * 1000).toLocaleString() : "未知";
      const lastUsed = p.last_used ? new Date(p.last_used * 1000).toLocaleString() : "從未使用";
      const shortId = p.credential_id ? p.credential_id.substring(0, 15) + "..." : "";
      html += `<tr><td><strong>${esc(p.nickname)}</strong></td><td>${created}</td><td>${lastUsed}</td><td><code title="${esc(p.credential_id)}">${esc(shortId)}</code></td>
        <td><button class="btn warn ghost" style="padding:2px 6px;font-size:10px" onclick="deletePasskey('${esc(p.credential_id)}')">刪除</button></td></tr>`;
    });
    html += `</tbody></table>`;
    wrap.innerHTML = html;
  } catch (e) {
    wrap.innerHTML = `<p style="color:var(--danger)">載入失敗: ${esc(e.message)}</p>`;
  }
}

async function deletePasskey(id) {
  if (!confirm("確定要刪除此通行密鑰？如果這是您唯一的密鑰，您將無法再次登入！")) return;
  try {
    await api(`/dashboard/api/auth/passkeys/${encodeURIComponent(id)}`, { method: "DELETE" });
    loadPasskeys();
  } catch (e) {
    alert("刪除失敗: " + e.message);
  }
}

// -------- Account CRUD JS Helpers --------
function showAddAccountModal() {
  $("#add-account-wrap").classList.remove("hidden");
  $("#addAcctError").textContent = "";
}

function hideAddAccountModal() {
  $("#add-account-wrap").classList.add("hidden");
}

function toggleNewAcctType() {
  const type = $("#new-acct-type").value;
  if (type === "json") {
    $("#new-acct-token-wrap").style.display = "block";
    $("#new-acct-token-wrap").classList.remove("hidden");
    $("#new-acct-rt-wrap").style.display = "none";
    $("#new-acct-rt-wrap").classList.add("hidden");
  } else {
    $("#new-acct-token-wrap").style.display = "none";
    $("#new-acct-token-wrap").classList.add("hidden");
    $("#new-acct-rt-wrap").style.display = "block";
    $("#new-acct-rt-wrap").classList.remove("hidden");
  }
}

async function submitAddAccount() {
  const type = $("#new-acct-type").value;
  const comment = $("#new-acct-comment").value.trim();
  $("#addAcctError").textContent = "";
  
  let payload = {};
  if (type === "json") {
    const rawJSON = $("#new-acct-token").value.trim();
    if (!rawJSON) {
      $("#addAcctError").textContent = "請貼上 JSON 內容";
      return;
    }
    try {
      payload = JSON.parse(rawJSON);
    } catch(e) {
      $("#addAcctError").textContent = "JSON 格式無效: " + e.message;
      return;
    }
  } else {
    const rt = $("#new-acct-rt").value.trim();
    if (!rt) {
      $("#addAcctError").textContent = "請輸入 Refresh Token";
      return;
    }
    payload = { refresh_token: rt };
  }
  
  if (comment) {
    payload.comment = comment;
  }
  
  try {
    const res = await api("/dashboard/api/accounts", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    
    if (res.success) {
      hideAddAccountModal();
      $("#new-acct-token").value = "";
      $("#new-acct-rt").value = "";
      $("#new-acct-comment").value = "";
      
      const d = await api("/dashboard/api/state");
      state.accounts = d.accounts || [];
      renderAccounts();
    } else {
      $("#addAcctError").textContent = "新增失敗: " + (res.error || "未知錯誤");
    }
  } catch (e) {
    $("#addAcctError").textContent = "提交失敗: " + e.message;
  }
}

async function toggleAccount(id, enabled) {
  try {
    await api(`/dashboard/api/accounts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ disabled: enabled })
    });
    const d = await api("/dashboard/api/state");
    state.accounts = d.accounts || [];
    renderAccounts();
  } catch (e) {
    alert("更新帳號狀態失敗: " + e.message);
  }
}

async function deleteAccount(id) {
  if (!confirm(`確定要刪除帳號 ${id} 嗎？此操作不可逆。`)) return;
  try {
    await api(`/dashboard/api/accounts/${encodeURIComponent(id)}`, {
      method: "DELETE"
    });
    const d = await api("/dashboard/api/state");
    state.accounts = d.accounts || [];
    renderAccounts();
  } catch (e) {
    alert("刪除帳號失敗: " + e.message);
  }
}

function authHeaders(){
  const headers = {"Content-Type":"application/json"};
  const key = $("#apiKey") ? $("#apiKey").value.trim() : "";
  if(key) {
    headers["Authorization"] = `Bearer ${key}`;
  }
  return headers;
}
function hasKey(){
  const key = $("#apiKey") ? $("#apiKey").value.trim() : "";
  return key.length > 0 || authState.authenticated;
}
async function api(p,o={}){
  const headers = {"Content-Type":"application/json", ...(o.headers||{})};
  const key = $("#apiKey") ? $("#apiKey").value.trim() : "";
  if(key) {
    headers["Authorization"] = `Bearer ${key}`;
  }
  const r=await fetch(p,{...o,headers});
  if(r.status===401) {
    checkAuth();
    throw new Error("驗證失敗");
  }
  if(!r.ok)throw new Error(await r.text());
  return r.json();
}

function setConn(ok,msg){$("#connStatus").innerHTML=`<span class="status-dot ${ok?'':'off'} ${ok===false?'err':''}"></span>${msg}`;}

// sidebar jump
$$(".sidebar a[data-jump]").forEach(el=>el.addEventListener("click",()=>{
  $$(".sidebar a[data-jump]").forEach(x=>x.classList.remove("active"));
  el.classList.add("active");
  document.getElementById(el.dataset.jump).scrollIntoView({behavior:"smooth",block:"start"});
}));

// -------- state --------
const state={routing:null,accounts:[],active:{},completed:[],metrics:null,latencySummary:null,logs:[],logSeq:-1,expanded:new Set()};
const LOG_MAX_VIEW=2000;let logLevelFilter="ALL";let reqFilterText="";

function fmtTime(ts){if(!ts)return"";return new Date(ts*1000).toLocaleTimeString();}
function fmtMs(s){if(s==null)return"—";return s<1?`${(s*1000).toFixed(0)}ms`:`${s.toFixed(2)}s`;}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}

function saveKey(){
  if(!$("#apiKey").value.trim()){setConn(false,"未輸入金鑰");return;}
  localStorage.setItem("kiro-dashboard-key",$("#apiKey").value.trim());connect();
}
function forgetKey(){
  localStorage.removeItem("kiro-dashboard-key");
  if($("#apiKey")) $("#apiKey").value="";
  disconnectSSE();
  setConn(false,"未連線");
}

// -------- SSE --------
let sseAbort=null;let metricsTimer=null;let sseReconnectTimer=null;
function disconnectSSE(){
  if(sseAbort){sseAbort.abort();sseAbort=null;}
  if(metricsTimer){clearInterval(metricsTimer);metricsTimer=null;}
  if(sseReconnectTimer){clearTimeout(sseReconnectTimer);sseReconnectTimer=null;}
}
function connect(){
  disconnectSSE();
  streamEvents();
  pullMetrics();metricsTimer=setInterval(pullMetrics,5000);
  pullLogs();
  initLatencyToggle();
  loadPasskeys();
  loadModelsDropdown();
}
async function streamEvents(){
  setConn(null,"連線中");
  const ctrl=new AbortController();
  sseAbort=ctrl;
  try{
    const r=await fetch("/dashboard/api/events",{headers:authHeaders(),credentials:"same-origin",signal:ctrl.signal});
    if(r.status===401){setConn(false,"驗證失敗");checkAuth();return;}
    if(!r.ok){setConn(false,"連線失敗: HTTP "+r.status);sseReconnectTimer=setTimeout(streamEvents,3000);return;}
    setConn(true,"已連線");
    const reader=r.body.getReader();const dec=new TextDecoder();let buf="";
    while(true){
      const{value,done}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      let idx;while((idx=buf.indexOf("\n\n"))>=0){
        const raw=buf.slice(0,idx);buf=buf.slice(idx+2);
        if(!raw.trim()||raw.startsWith(":"))continue;
        let ev="message";let data="";
        raw.split("\n").forEach(line=>{
          if(line.startsWith("event: "))ev=line.slice(7);
          else if(line.startsWith("data: "))data+=line.slice(6);
        });
        try{handleEvent(ev,JSON.parse(data));}catch(e){console.warn("[SSE] handleEvent error:",ev,e);}
      }
    }
    setConn(false,"串流已關閉");sseReconnectTimer=setTimeout(streamEvents,2000);
  }catch(e){
    if(e.name==="AbortError")return;
    setConn(false,"錯誤: "+e.message);sseReconnectTimer=setTimeout(streamEvents,3000);
  }
}

function handleEvent(ev,d){
  if(ev==="snapshot"){
    state.routing=d.routing;state.accounts=d.accounts||[];
    (d.active_requests||[]).forEach(r=>state.active[r.id]=r);
    state.completed=d.completed_requests||[];
    writeRoutingForm(state.routing);if(d.throttle)writeThrottleForm(d.throttle);renderAll();
  }else if(ev==="request_started"){state.active[d.id]=d;renderRequests();}
   else if(ev==="attempt"){if(state.active[d.id]){state.active[d.id]=d;patchRow(d)||renderRequests();}}
   else if(ev==="request_finished"){
     delete state.active[d.id];state.completed.unshift(d);
     if(state.completed.length>200)state.completed.pop();
     renderRequests();
   }
   else if(ev==="stream_progress"){
     const r=state.active[d.id];if(r){
        if(d.ttft_s!=null)r.ttft_s=d.ttft_s;if(d.tps!=null)r.tps=d.tps;
        if(d.output_tokens!=null)r.output_tokens=d.output_tokens;
        if(d.content_delta){r.chunks=r.chunks||[];r.chunks.push(d.content_delta);if(r.chunks.length>80)r.chunks=r.chunks.slice(-80);}
        const tr=document.querySelector(`#requestsWrap tr[data-id="${d.id}"]`);
        if(tr){
          const cells=tr.querySelectorAll("td");
          if(cells.length>=8){
            cells[5].textContent=r.ttft_s!=null?r.ttft_s.toFixed(2)+"s":"…";
            cells[6].textContent=r.tps!=null?r.tps.toFixed(1)+" t/s":"…";
            cells[7].textContent=r.output_tokens??"…";
          }
          const exp=tr.nextElementSibling;
          if(exp&&exp.classList.contains("exp")&&d.content_delta){
            const pre=exp.querySelector("pre.code.streamlive");
            if(pre){pre.textContent+=d.content_delta;pre.scrollTop=pre.scrollHeight;}
          }
        }else{renderRequests();}
     }
   }
   else if(ev==="latency_summary"){state.latencySummary=d;renderLatency();}
   else if(ev==="log"){pushLog(d);}
}

function patchRow(r){
  const tr=document.querySelector(`#requestsWrap tr[data-id="${r.id}"]`);
  if(!tr)return false;
  const newHtml=reqRow(r);
  const tmp=document.createElement("tbody");tmp.innerHTML=newHtml;
  const fresh=tmp.firstElementChild;
  tr.replaceWith(fresh);
  return true;
}

async function pullMetrics(){
  try{const m=await api("/dashboard/api/metrics");state.metrics=m;renderStripe();}catch{}
}
async function pullLogs(){
  try{const r=await api(`/dashboard/api/logs?since=${state.logSeq}`);
    (r.entries||[]).forEach(pushLog);
  }catch{}
}

function pushLog(e){
  if(e.seq!=null&&e.seq<=state.logSeq)return;
  if(e.seq!=null)state.logSeq=e.seq;
  state.logs.push(e);if(state.logs.length>LOG_MAX_VIEW)state.logs.shift();
  renderLogLine(e);
}

// -------- rendering --------
function renderAll(){try{renderStripe();}catch(e){console.warn("[render] renderStripe:",e);}
try{renderAccounts();}catch(e){console.warn("[render] renderAccounts:",e);}
try{renderRequests();}catch(e){console.warn("[render] renderRequests:",e);}
try{renderLatency();}catch(e){console.warn("[render] renderLatency:",e);}}

function renderStripe(){
  const m=state.metrics;
  if(!m){return;}
  const rps=m.count/Math.max(1,m.window_s);
  $("#mRps").textContent=rps.toFixed(2);
  $("#mP50").textContent=fmtMs(m.p50);
  $("#mP95").textContent=fmtMs(m.p95);
  const errPct=m.count?(m.errors/m.count*100):0;
  $("#mErr").textContent=errPct.toFixed(1)+"%";
  $("#mActive").textContent=Object.keys(state.active).length;
  $("#stripeMeta").textContent=`${m.count} 次請求 · ${m.errors} 次錯誤 · 窗口=${m.window_s}秒`;
  drawSpark("#sparkRps",(m.series||[]).map(s=>s.count),"#1d6f68");
  drawSpark("#sparkErr",(m.series||[]).map(s=>s.errors),"#9b2d20");
}
function drawSpark(sel,data,color){
  const svg=$(sel);if(!svg||!data||!data.length){svg.innerHTML="";return;}
  const w=200,h=36;const max=Math.max(1,...data);const step=w/Math.max(1,data.length-1);
  const pts=data.map((v,i)=>`${(i*step).toFixed(1)},${(h-(v/max)*(h-2)-1).toFixed(1)}`).join(" ");
  svg.innerHTML=`<polyline fill="none" stroke="${color}" stroke-width="1.4" points="${pts}"/>`;
}

function renderAccounts(){
  const el=$("#accounts");if(!state.accounts.length){el.innerHTML=`<p style="color:var(--muted)">尚無帳號資料。</p>`;return;}
  $("#accountCount").textContent=state.accounts.length;
  el.innerHTML=state.accounts.map(a=>{
    const cur=a.is_current?'current':'';
    const cool=a.cooldown_remaining_s>0
      ? `<div class="cooldown">冷卻中 ${a.cooldown_remaining_s}秒 / ${a.cooldown_total_s}秒（第 ${a.backoff_tier} 級）</div>`:"";
    const lastErr=a.last_error_reason?`<div style="font-size:10.5px;color:var(--muted)">last: ${esc(a.last_error_reason)} (${a.last_error_status||"-"})</div>`:"";
    
    let tagHtml = `<span class="tag ${a.is_current?'on':''} ${a.failures>0?'err':''}">${a.is_current?'使用中':(a.failures>0?'冷卻中':'待命')}</span>`;
    if(!a.enabled) {
      tagHtml = `<span class="tag dis">已停用</span>`;
    }

    return `<div class="acct ${cur}" style="${a.enabled?'':'opacity:0.65;background:#f5f5f5'}">
      <div class="name"><span>${esc(a.display_id)}</span>${tagHtml}</div>
      <dl><dt>總計</dt><dt>成功</dt><dt>失敗</dt>
        <dd>${a.stats.total_requests}</dd><dd>${a.stats.successful_requests}</dd><dd>${a.stats.failed_requests}</dd></dl>
      ${cool}${lastErr}
      <div style="margin-top:8px;display:flex;justify-content:flex-end;gap:4px">
        <button class="btn ghost" style="padding:2px 6px;font-size:10px" onclick="toggleAccount('${esc(a.id)}', ${a.enabled})">${a.enabled?'停用':'啟用'}</button>
        <button class="btn warn ghost" style="padding:2px 6px;font-size:10px" onclick="deleteAccount('${esc(a.id)}')">刪除</button>
      </div>
    </div>`;
  }).join("");
}

function reqRow(r){
  const statusCls=r.status==="completed"?"ok":(r.status==="active"?"active":(r.status==="client_disconnected"?"dis":"err"));
  const ttft=r.ttft_s!=null?fmtMs(r.ttft_s):"—";const tps=r.tps!=null?`${r.tps.toFixed(1)}t/s`:"—";
  const trim=r.trim_before_messages?`${r.trim_before_messages}→${r.trim_after_messages} msg`:"";
  const attempts=(r.attempts||[]).length;
  const totalCell=r.total_s!=null?fmtMs(r.total_s):(r.trace&&r.trace.total_ms!=null?(r.trace.total_ms/1000).toFixed(2)+"s":"—");
  const traceBadge=r.trace?`<span class="tag" style="font-size:9.5px;padding:0 4px;background:#1d6f68;color:#fff" title="auth=${(r.trace.auth_ms||0).toFixed(0)} gate=${(r.trace.gate_wait_ms||0).toFixed(0)} up=${(r.trace.upstream_connect_ms||0).toFixed(0)} ttft=${(r.trace.ttft_ms||0).toFixed(0)} stream=${(r.trace.streaming_ms||0).toFixed(0)} ms">trace</span>`:"";
  return `<tr class="row" data-id="${esc(r.id)}"><td>${fmtTime(r.started_at)}</td>
    <td><span class="statusbadge ${statusCls}">${esc(r.status)}</span> ${traceBadge}</td>
    <td>${esc(r.api_format)} ${r.stream?"⋯":""}</td>
    <td>${esc(r.original_model)}${r.original_model!==r.active_model?`→${esc(r.active_model)}`:""}</td>
    <td>${totalCell}</td>
    <td>${ttft}</td><td>${tps}</td><td>${r.output_tokens??"—"}</td><td>${attempts}</td><td>${esc(trim)}</td>
    <td>${r.error?`<span class="statusbadge err">${esc(r.error.slice(0,60))}</span>`:""}</td></tr>`;
}

function renderRequests(){
  const activeRows=Object.values(state.active);
  $("#activeCount").textContent=`${activeRows.length} 進行中`;
  const f=reqFilterText.toLowerCase().trim();
  const all=[...activeRows,...state.completed].sort((a,b)=>b.started_at-a.started_at);
  const rows=all.filter(r=>!f||JSON.stringify(r).toLowerCase().includes(f));
  const el=$("#requestsWrap");
  if(!rows.length){el.innerHTML=`<p style="color:var(--muted)">${f?"沒有符合篩選的請求。":"尚無請求。"}</p>`;return;}
  el.innerHTML=`<table class="reqs"><thead><tr><th>開始</th><th>狀態</th><th>格式</th><th>模型</th><th>總耗時</th><th>首字</th><th>速度</th><th>token</th><th>重試</th><th>裁剪</th><th>錯誤</th></tr></thead>
    <tbody>${rows.slice(0,500).map(r=>reqRow(r)).join("")}</tbody></table>`;
  wireRowExpand();
}

function renderLatency(){
  const m=state.latencySummary;
  if(!m){$("#latencyMeta").textContent="未啟用";$("#latencyTable").innerHTML=`<p style="color:var(--muted)">設 <code>LATENCY_TRACING=true</code> 啟用後，每 5 秒更新。</p>`;return;}
  const ps=m.per_stage||{};
  const order=["auth_ms","gate_wait_ms","upstream_connect_ms","ttft_ms","streaming_ms"];
  const labels={auth_ms:"Auth (取 token)",gate_wait_ms:"Gate Wait (節流等候)",upstream_connect_ms:"Upstream Connect (上游連線)",ttft_ms:"TTFT (首 token)",streaming_ms:"Streaming (首 token 後)"};
  $("#latencyMeta").textContent=`窗口 ${m.window_s}s · 樣本 ${m.count}`;
  let rows=order.filter(n=>ps[n]).map(n=>{
    const s=ps[n];
    const max=Math.max(...order.filter(k=>ps[k]).map(k=>ps[k].p95));
    const barW=max>0?(s.p95/max*100):0;
    return`<tr><td>${labels[n]}</td><td style="text-align:right">${s.count}</td><td style="text-align:right">${s.p50.toFixed(1)} ms</td><td style="text-align:right">${s.p95.toFixed(1)} ms</td><td><div style="background:#efe9dc;border-radius:3px;height:14px;width:100%;position:relative"><div style="background:var(--accent);height:100%;width:${barW.toFixed(1)}%;border-radius:3px"></div></div></td></tr>`;
  }).join("");
  if(!rows){$("#latencyTable").innerHTML=`<p style="color:var(--muted)">尚無延遲資料。發幾個請求後會出現。</p>`;return;}
  $("#latencyTable").innerHTML=`<table class="reqs"><thead><tr><th>階段</th><th>樣本數</th><th>P50</th><th>P95</th><th style="width:35%">P95 視覺化</th></tr></thead><tbody>${rows}</tbody></table>`;
}

$("#reqFilter").addEventListener("input",e=>{reqFilterText=e.target.value;renderRequests();});

function wireRowExpand(){
  $("#requestsWrap").querySelectorAll("tr.row").forEach(tr=>{
    const id=tr.dataset.id;
    if(state.expanded.has(id)){
      const r=state.active[id]||state.completed.find(x=>x.id===id);
      if(r){
        const exp=document.createElement("tr");exp.className="exp";exp.dataset.expFor=id;
        exp.innerHTML=`<td colspan="11">${renderDetail(r)}</td>`;
        tr.after(exp);
      }
    }
  });
}
// Event delegation — survives DOM rebuilds from stream_progress/attempt events
$("#requestsWrap").addEventListener("click",e=>{
  const tr=e.target.closest("tr.row");if(!tr)return;
  const id=tr.dataset.id;
  const next=tr.nextElementSibling;
  if(next&&next.classList.contains("exp")){next.remove();state.expanded.delete(id);return;}
  const r=state.active[id]||state.completed.find(x=>x.id===id);
  if(!r)return;
  const exp=document.createElement("tr");exp.className="exp";exp.dataset.expFor=id;
  exp.innerHTML=`<td colspan="11">${renderDetail(r)}</td>`;
  tr.after(exp);
  state.expanded.add(id);
});

function renderDetail(r){
  const attempts=(r.attempts||[]).map(a=>`<div>${esc(a.model)} · ${a.account_id||"-"} · ${a.http_status||"-"} · ${esc(a.status)}${a.error?` · ${esc(a.error)}`:""}</div>`).join("");
  const payloads=Object.entries(r.payloads||{}).map(([n,b])=>{
    const big=b&&b.length>40000;
    return `<details class="payload"><summary>${esc(n)}${big?" (large)":""}</summary>${big?`<p style="color:var(--muted)">${b.length} chars — open to load</p><button class="btn" onclick="this.nextElementSibling.classList.remove('hidden');this.remove()">Show</button><pre class="code hidden">${esc(b)}</pre>`:`<pre class="code">${esc(b)}</pre>`}</details>`;
  }).join("");
  const isActive=!!state.active[r.id];
  const chunkText=(r.chunks||[]).join("");
  let chunks="";
  if(isActive&&chunkText){
    chunks=`<div style="margin-top:4px"><strong>串流內容</strong> <span style="color:var(--muted);font-size:11px">(即時更新)</span></div><pre class="code streamlive" style="max-height:300px;overflow:auto">${esc(chunkText)}</pre>`;
  }else if(isActive){
    chunks=`<div style="margin-top:4px"><strong>串流內容</strong> <span style="color:var(--muted);font-size:11px">(等待中…)</span></div><pre class="code streamlive" style="max-height:300px;overflow:auto"></pre>`;
  }else if((r.chunks||[]).length){
    chunks=`<details class="payload"><summary>stream chunks (${r.chunks.length})</summary><pre class="code">${esc(chunkText)}</pre></details>`;
  }
  let trace="";
  if(r.trace){
    const t=r.trace;
    const fmt=v=>v==null?"—":v.toFixed(1)+"ms";
    const stages=[
      {k:"auth_ms",label:"auth"},
      {k:"gate_wait_ms",label:"gate"},
      {k:"upstream_connect_ms",label:"connect"},
      {k:"ttft_ms",label:"ttft"},
      {k:"streaming_ms",label:"stream"},
    ];
    const total=t.total_ms||0;
    const segs=stages.map(s=>{
      const v=t[s.k];if(v==null||total<=0)return"";
      const pct=Math.max(1,(v/total*100));
      const colors={auth:"#c45f2c",gate:"#9b2d20",connect:"#1d6f68",ttft:"#5a4fcf",stream:"#3a8a3f"};
      return `<div title="${s.label}=${fmt(v)}" style="background:${colors[s.label]};width:${pct.toFixed(1)}%;color:#fff;padding:2px 4px;font-size:10px;text-align:center;overflow:hidden;white-space:nowrap">${s.label} ${fmt(v)}</div>`;
    }).join("");
    trace=`<div style="margin-bottom:6px"><strong>Latency Trace</strong> · 總計 ${fmt(t.total_ms)}
      <div style="display:flex;margin-top:4px;border-radius:4px;overflow:hidden;border:1px solid var(--line);min-height:20px">${segs}</div>
      <div style="font-size:10.5px;color:var(--muted);margin-top:3px">auth=${fmt(t.auth_ms)} · gate_wait=${fmt(t.gate_wait_ms)} · upstream_connect=${fmt(t.upstream_connect_ms)} · ttft=${fmt(t.ttft_ms)} · streaming=${fmt(t.streaming_ms)}</div></div>`;
  }
  const resp=r.response?`<details class="payload"><summary>response</summary><pre class="code">${esc(r.response)}</pre></details>`:"";
  return `<div style="padding:6px 4px"><div style="color:var(--muted);margin-bottom:4px">id=${esc(r.id)} · reason=${esc(r.routing_reason||"")}</div>
    ${trace}<div style="margin-bottom:4px"><strong>嘗試紀錄</strong>${attempts||" —"}</div>${payloads}${chunks}${resp}</div>`;
}

// -------- logs UI --------
$$(".filter-chip").forEach(c=>c.addEventListener("click",()=>{
  $$(".filter-chip").forEach(x=>x.classList.remove("on"));c.classList.add("on");
  logLevelFilter=c.dataset.level;redrawLogs();
}));
$("#logSearch").addEventListener("input",redrawLogs);
function clearLogView(){$("#logBody").innerHTML="";}
function keepLine(e){
  if(logLevelFilter==="WARNING"&&!["WARNING","ERROR","CRITICAL"].includes(e.level))return false;
  if(logLevelFilter==="ERROR"&&!["ERROR","CRITICAL"].includes(e.level))return false;
  const q=$("#logSearch").value.toLowerCase();
  if(q&&!(e.msg||"").toLowerCase().includes(q))return false;
  return true;
}
function renderLogLine(e){
  if(!keepLine(e))return;
  const body=$("#logBody");const d=document.createElement("div");
  d.className="line lvl-"+(e.level||"INFO");
  const ts=new Date((e.ts||0)*1000).toLocaleTimeString();
  d.textContent=`${ts} ${e.level||""} ${e.msg||""}`;
  body.appendChild(d);
  while(body.childElementCount>LOG_MAX_VIEW)body.firstElementChild.remove();
  if($("#logAutoScroll").checked)body.scrollTop=body.scrollHeight;
}
function redrawLogs(){const b=$("#logBody");b.innerHTML="";state.logs.forEach(renderLogLine);}

// -------- routing form --------
const DEFAULT_MODELS = [
  "claude-haiku-4.5", "claude-sonnet-4.7", "claude-opus-4.7", "claude-opus-4.8", "claude-opus-4.6"
];

function populateManualModelDropdown(modelsList) {
  const select = $("#manualModel");
  if(!select) return;
  const currentVal = select.value;
  const models = Array.from(new Set([...DEFAULT_MODELS, ...(modelsList || []).map(m => m.modelId || m)]));
  select.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
  if(currentVal && models.includes(currentVal)) {
    select.value = currentVal;
  }
}

async function loadModelsDropdown() {
  try {
    const d = await api("/dashboard/api/models");
    const models = d.models || [];
    populateManualModelDropdown(models);
  } catch(e) {
    populateManualModelDropdown([]);
  }
}

function previewRouting() {
  const form = readRoutingForm();
  $("#routing-preview-wrap").classList.remove("hidden");
  const results = $("#routing-preview-results");
  
  const testModels = ["claude-opus-4.7", "claude-opus-4.8", "claude-sonnet-4.7", "claude-haiku-4.5"];
  let html = `<table class="reqs" style="margin-top:4px;width:100%"><thead><tr><th>原始請求模型</th><th>路由後目標模型</th><th>自訂重導向</th></tr></thead><tbody>`;
  
  testModels.forEach(m => {
    let target = m;
    let via = "直通 (Passthrough)";
    if (form.enabled) {
      if (form.mode === "manual") {
        target = form.manual_model;
        via = `手動覆寫 (Manual Override)`;
      } else if (form.mode === "redirect") {
        if (form.redirects && form.redirects[m]) {
          target = form.redirects[m];
          via = `規則映射: ${m} → ${target}`;
        } else {
          via = "直通 (Passthrough)";
        }
      }
    }
    html += `<tr><td><code>${esc(m)}</code></td><td><span class="statusbadge ok">${esc(target)}</span></td><td><span style="color:var(--muted)">${esc(via)}</span></td></tr>`;
  });
  html += `</tbody></table>`;
  results.innerHTML = html;
}

function writeRoutingForm(r){if(!r)return;
  $("#enabled").checked=r.enabled;$("#mode").value=r.mode;
  populateManualModelDropdown([]); // ensure dropdown populated before setting value
  $("#manualModel").value=r.manual_model;
  $("#redirects").value=JSON.stringify(r.redirects,null,2);
  $("#fallbackModels").value=r.fallback_models.join(", ");
  $("#fallbackEnabled").checked=r.fallback_enabled;$("#safeFallback").checked=r.safe_fallback_to_original;
  $("#captureContent").checked=r.capture_content;}
function readRoutingForm(){let red={};try{red=JSON.parse($("#redirects").value||"{}")}catch(e){throw new Error("重導向規則必須是合法 JSON");}
  return{enabled:$("#enabled").checked,mode:$("#mode").value,manual_model:$("#manualModel").value,redirects:red,
    fallback_enabled:$("#fallbackEnabled").checked,
    fallback_models:$("#fallbackModels").value.split(",").map(v=>v.trim()).filter(Boolean),
    safe_fallback_to_original:$("#safeFallback").checked,capture_content:$("#captureContent").checked};}
async function applyRouting(){try{const d=await api("/dashboard/api/routing",{method:"PUT",body:JSON.stringify(readRoutingForm())});
  writeRoutingForm(d.routing);$("#saveResult").textContent="已套用 "+new Date().toLocaleTimeString();}catch(e){$("#saveResult").textContent=e.message;}}
async function resetRouting(){await api("/dashboard/api/routing/reset",{method:"POST",body:"{}"});$("#saveResult").textContent="已重設";}
async function clearMonitor(){await api("/dashboard/api/monitor/clear",{method:"POST",body:"{}"});state.completed=[];renderRequests();}

// -------- latency tracing toggle --------
async function initLatencyToggle(){
  try{const d=await api("/dashboard/api/latency-tracing");$("#latencyToggle").checked=d.enabled;}catch{}
}
$("#latencyToggle").addEventListener("change",async()=>{
  try{await api("/dashboard/api/latency-tracing",{method:"PUT",body:JSON.stringify({enabled:$("#latencyToggle").checked})});}
  catch(e){$("#latencyToggle").checked=!$("#latencyToggle").checked;}
});

async function restartGateway(){if(!confirm("確定要重啟 Gateway？進行中的請求會中斷。"))return;
  try{await api("/dashboard/api/restart",{method:"POST",body:"{}"});setConn(null,"重啟中…");setTimeout(()=>location.reload(),3000);}catch(e){alert(e.message);}}

// -------- throttle form --------
function writeThrottleForm(t){if(!t)return;
  $("#throttleStrategy").value=t.throttle_fast_fail?"fastfail":"retry";
  $("#burstEnabled").checked=t.enabled;
  if(t.gate){
    $("#gateGap").textContent=t.gate.current_gap_ms+"ms";
    $("#gateSuccesses").textContent=t.gate.consecutive_successes;
    $("#gate429s").textContent=t.gate.consecutive_429s;
    $("#maxGapMs").value=t.gate.max_gap_ms;
  }}
function readThrottleForm(){return{
  throttle_fast_fail:$("#throttleStrategy").value==="fastfail",
  enabled:$("#burstEnabled").checked,
  max_gap_ms:parseInt($("#maxGapMs").value)||8000};}
async function applyThrottle(){try{const d=await api("/dashboard/api/throttle",{method:"PUT",body:JSON.stringify(readThrottleForm())});
  writeThrottleForm(d.throttle);$("#throttleResult").textContent="已套用 "+new Date().toLocaleTimeString();}catch(e){$("#throttleResult").textContent=e.message;}}

// -------- models panel --------
async function loadModels(){
  $("#modelsStatus").textContent="載入中…";
  try{
    const d=await api("/dashboard/api/models");
    if(d.error){$("#modelsStatus").textContent="錯誤: "+d.error;$("#modelsWrap").innerHTML=`<p style="color:var(--danger)">${esc(d.error)}</p>`;return;}
    const models=d.models||[];
    const acct=d.account_id?` · 帳號 ${esc(d.account_id)}`:"";
    $("#modelsStatus").textContent=`${models.length} 個模型${acct} · ${new Date().toLocaleTimeString()}`;
    if(!models.length){$("#modelsWrap").innerHTML=`<p style="color:var(--muted)">遠端未回傳任何模型。</p>`;return;}
    let html=`<table class="reqs"><thead><tr><th>#</th><th>模型 ID</th><th>顯示名稱</th><th>最大輸入</th><th>最大輸出</th></tr></thead><tbody>`;
    models.forEach((m,i)=>{
      const limits=m.tokenLimits||{};
      const maxIn=limits.maxInputTokens!=null?Number(limits.maxInputTokens).toLocaleString():"—";
      const maxOut=limits.maxOutputTokens!=null?Number(limits.maxOutputTokens).toLocaleString():"—";
      html+=`<tr><td>${i+1}</td><td>${esc(m.modelId||"?")}</td><td>${esc(m.displayName||m.modelName||"?")}</td><td style="text-align:right">${maxIn}</td><td style="text-align:right">${maxOut}</td></tr>`;
    });
    html+=`</tbody></table>`;
    $("#modelsWrap").innerHTML=html;
  }catch(e){$("#modelsStatus").textContent="錯誤: "+e.message;}
}

// Bootstrap auth check on load
checkAuth();
</script>
</body></html>
"""
