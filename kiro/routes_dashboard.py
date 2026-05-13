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


async def verify_dashboard_api_key(
    authorization: Optional[str] = Security(dashboard_auth_header),
    x_api_key: Optional[str] = Security(dashboard_x_api_key_header),
) -> bool:
    """
    Verify dashboard API key.

    Args:
        authorization: Authorization header value.
        x_api_key: x-api-key header value.

    Returns:
        True when authenticated.

    Raises:
        HTTPException: 401 when the API key is missing or invalid.
    """
    if _is_valid_dashboard_api_key(authorization, x_api_key):
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing dashboard API key")


async def optional_dashboard_api_key(
    authorization: Optional[str] = Security(dashboard_auth_header),
    x_api_key: Optional[str] = Security(dashboard_x_api_key_header),
) -> bool:
    """
    Check dashboard API authentication without raising 401.

    This keeps unauthenticated dashboard pages from producing repeated 401 log
    noise while still withholding routing and monitoring data.

    Args:
        authorization: Authorization header value.
        x_api_key: x-api-key header value.

    Returns:
        True when the supplied dashboard API key is valid.
    """
    return _is_valid_dashboard_api_key(authorization, x_api_key)


def _is_valid_dashboard_api_key(
    authorization: Optional[str],
    x_api_key: Optional[str],
) -> bool:
    """
    Validate dashboard API key header values.

    Args:
        authorization: Authorization header value.
        x_api_key: x-api-key header value.

    Returns:
        True when either supported auth header contains the proxy API key.
    """
    if x_api_key and x_api_key == PROXY_API_KEY:
        return True
    if authorization and authorization == f"Bearer {PROXY_API_KEY}":
        return True
    return False


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
    # Check if request is from localhost to auto-bring cred
    host = request.headers.get("host", "")
    client_host = request.client.host if request.client else ""
    
    is_localhost = (
        client_host in ("127.0.0.1", "::1", "localhost") or
        "127.0.0.1" in host or
        "localhost" in host or
        "::1" in host
    )
    
    # Debug log for localhost detection (only if not localhost to see why)
    if not is_localhost:
        logger.debug(f"Dashboard access from non-local source: client={client_host}, host_header={host}")

    html = DASHBOARD_HTML
    if is_localhost:
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
        try:
            snap = control_panel.snapshot()
            snap["accounts"] = (
                account_manager.get_accounts_snapshot() if account_manager else []
            )
            yield f"event: snapshot\ndata: {_json.dumps(snap, default=str)}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {evt['event']}\ndata: {_json.dumps(evt['data'], default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            control_panel.unsubscribe(on_panel_event)
            log_buffer.unsubscribe(on_log_event)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    .btn+.btn{margin-left:6px}
    .accounts{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
    .acct{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#fffcf3;font-size:12px}
    .acct.current{border-color:var(--accent-2);background:rgba(29,111,104,.06)}
    .acct .name{font-weight:700;display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
    .tag{font-size:10px;padding:1px 6px;border-radius:999px;background:var(--line);color:var(--muted);
      text-transform:uppercase;letter-spacing:.08em}
    .tag.on{background:var(--accent-2);color:#fff}
    .tag.err{background:var(--danger);color:#fff}
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
    @media (max-width: 960px){.app{grid-template-columns:1fr}nav.sidebar{position:static;height:auto}}
  </style>
</head>
<body>
<div class="app">
  <nav class="sidebar">
    <h1>Kiro Gateway</h1>
    <div class="ver">vAPP_VERSION_PLACEHOLDER · <span id="connStatus"><span class="status-dot off"></span>未連線</span></div>
    <a data-jump="panel-status" class="active">總覽</a>
    <a data-jump="panel-routing">路由設定</a>
    <a data-jump="panel-throttle">流量控制</a>
    <a data-jump="panel-accounts">帳號</a>
    <a data-jump="panel-active">進行中</a>
    <a data-jump="panel-history">歷史紀錄</a>
    <a data-jump="panel-logs">即時日誌</a>
    <a data-jump="panel-models">遠端模型</a>
    <div class="key">
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
        <div class="spark"><svg id="sparkRps" viewBox="0 0 200 36" preserveAspectRatio="none"></svg><svg id="sparkErr" viewBox="0 0 200 36" preserveAspectRatio="none"></svg></div>
      </div>
      <p style="font-size:11px;color:var(--muted);margin-top:8px">滾動窗口統計近 5 分鐘的請求量、延遲百分位數和錯誤率。火花圖顯示每 5 秒一個桶的趨勢。</p>
    </section>

    <section id="panel-routing" class="panel">
      <header><h2>路由設定</h2><div class="toolbar">
        <button class="btn" onclick="quickSwap('claude-opus-4.6')">強制 4.6</button>
        <button class="btn" onclick="quickSwap('claude-opus-4.7')">強制 4.7</button>
        <button class="btn warn" onclick="resetRouting()">重設</button>
      </div></header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">即時調整模型路由策略。「直通」不做任何修改；「手動」強制使用指定模型；「重導向」依 JSON 規則映射。啟用降級後，主模型失敗會自動嘗試備用模型。</p>
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px">
        <label class="chip"><input id="enabled" type="checkbox"> 啟用路由</label>
        <label class="chip"><input id="safeFallback" type="checkbox"> 失敗時重試原模型</label>
        <label class="chip"><input id="fallbackEnabled" type="checkbox"> 模型降級</label>
        <label class="chip"><input id="captureContent" type="checkbox"> 擷取請求內容</label>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px">
        <div><label style="font-size:10.5px;color:var(--muted)">模式</label>
          <select id="mode" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"><option value="passthrough">直通</option><option value="manual">手動</option><option value="redirect">重導向</option></select></div>
        <div><label style="font-size:10.5px;color:var(--muted)">手動指定模型</label>
          <input id="manualModel" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px" placeholder="claude-opus-4.6"></div>
        <div><label style="font-size:10.5px;color:var(--muted)">降級模型（逗號分隔）</label>
          <input id="fallbackModels" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"></div>
      </div>
      <label style="font-size:10.5px;color:var(--muted);display:block;margin-top:8px">重導向規則 JSON</label>
      <textarea id="redirects" style="width:100%;min-height:64px;padding:6px;border:1px solid var(--line);border-radius:6px;font:12px 'SF Mono',Menlo,monospace"></textarea>
      <div style="margin-top:8px"><button class="btn primary" onclick="applyRouting()">套用</button><span id="saveResult" style="margin-left:10px;color:var(--muted)"></span></div>
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
      <header><h2>帳號 <span id="accountCount" class="tag">0</span></h2></header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">顯示所有 API 帳號的即時狀態。「使用中」為當前輪轉到的帳號；「冷卻中」表示該帳號因錯誤觸發指數退避，倒數結束後自動恢復。</p>
      <div id="accounts" class="accounts"><p style="color:var(--muted)">等待資料…</p></div>
    </section>

    <section id="panel-active" class="panel">
      <header><h2>進行中的請求</h2><span id="activeCount" class="tag">0</span></header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">正在處理的串流/非串流請求。點擊列可展開查看嘗試明細、路由原因和原始 payload。「首字」= 首 token 延遲 (TTFT)，「速度」= 每秒 token 數 (TPS)。</p>
      <div id="activeWrap"><p style="color:var(--muted)">目前沒有進行中的請求。</p></div>
    </section>

    <section id="panel-history" class="panel">
      <header><h2>歷史紀錄</h2>
        <div class="toolbar">
          <input id="histFilter" class="search" placeholder="篩選：模型 / 狀態 / 帳號 / 錯誤">
          <button class="btn ghost" onclick="clearMonitor()">清除</button>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">最近 200 筆已完成請求。支援全文篩選（模型名稱、狀態碼、帳號 ID、錯誤訊息）。點擊列展開詳情。「裁剪」欄顯示 context window 自動截斷前後的訊息數。</p>
      <div id="historyWrap" style="max-height:520px;overflow:auto"></div>
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
          <input class="filter" id="logSearch" placeholder="搜尋日誌（子字串，不分大小寫）">
          <label><input id="logAutoScroll" type="checkbox" checked> 自動捲動</label>
          <button class="btn" onclick="clearLogView()">清除畫面</button>
        </header>
        <div class="body" id="logBody"></div>
      </div>
    </section>

    <section id="panel-models" class="panel">
      <header><h2>遠端模型</h2>
        <div class="toolbar">
          <button class="btn primary" onclick="loadModels()">重新載入</button>
          <span id="modelsStatus" style="font-size:11px;color:var(--muted)"></span>
        </div>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">查詢 Kiro 遠端 API 目前可用的模型清單，包含各模型的輸入/輸出 token 上限。可用於確認帳號權限和模型可用性。</p>
      <div id="modelsWrap"><p style="color:var(--muted)">點擊「重新載入」查詢遠端模型。</p></div>
    </section>
  </main>
</div>

<script>
const $=s=>document.querySelector(s);const $$=s=>document.querySelectorAll(s);
const autoKey=window.KIRO_AUTO_KEY;let storedKey=localStorage.getItem("kiro-dashboard-key");
if(autoKey&&(!storedKey||storedKey.length<3)){localStorage.setItem("kiro-dashboard-key",autoKey);storedKey=autoKey;}
$("#apiKey").value=storedKey||"";

function authHeaders(){return{"Authorization":`Bearer ${$("#apiKey").value.trim()}`,"Content-Type":"application/json"};}
function hasKey(){return $("#apiKey").value.trim().length>0;}
async function api(p,o={}){if(!hasKey())throw new Error("請先輸入 API 金鑰");
  const r=await fetch(p,{...o,headers:{...authHeaders(),...(o.headers||{})}});
  if(r.status===401)throw new Error("驗證失敗");
  if(!r.ok)throw new Error(await r.text());
  return r.json();}

function setConn(ok,msg){$("#connStatus").innerHTML=`<span class="status-dot ${ok?'':'off'} ${ok===false?'err':''}"></span>${msg}`;}

// sidebar jump
$$(".sidebar a[data-jump]").forEach(el=>el.addEventListener("click",()=>{
  $$(".sidebar a[data-jump]").forEach(x=>x.classList.remove("active"));
  el.classList.add("active");
  document.getElementById(el.dataset.jump).scrollIntoView({behavior:"smooth",block:"start"});
}));

// -------- state --------
const state={routing:null,accounts:[],active:{},completed:[],metrics:null,logs:[],logSeq:-1,expandedActive:new Set(),expandedCompleted:new Set()};
const LOG_MAX_VIEW=2000;let logLevelFilter="ALL";let histFilterText="";

function fmtTime(ts){if(!ts)return"";return new Date(ts*1000).toLocaleTimeString();}
function fmtMs(s){if(s==null)return"—";return s<1?`${(s*1000).toFixed(0)}ms`:`${s.toFixed(2)}s`;}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}

function saveKey(){if(!hasKey()){setConn(false,"未輸入金鑰");return;}
  localStorage.setItem("kiro-dashboard-key",$("#apiKey").value.trim());connect();}
function forgetKey(){localStorage.removeItem("kiro-dashboard-key");$("#apiKey").value="";if(es)es.close();setConn(false,"未連線");}

// -------- SSE --------
let es=null;
function connect(){
  if(es)es.close();
  if(!hasKey()){setConn(false,"未輸入金鑰");return;}
  const url=`/dashboard/api/events?_auth=${encodeURIComponent($("#apiKey").value.trim())}`;
  // fall back to fetch+reader because EventSource can't set headers
  streamEvents();
  pullMetrics();setInterval(pullMetrics,5000);
  pullLogs();
}
async function streamEvents(){
  setConn(null,"連線中");
  try{
    const r=await fetch("/dashboard/api/events",{headers:authHeaders()});
    if(r.status===401){setConn(false,"驗證失敗");return;}
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
        try{handleEvent(ev,JSON.parse(data));}catch(e){}
      }
    }
    setConn(false,"串流已關閉");setTimeout(streamEvents,2000);
  }catch(e){setConn(false,"錯誤: "+e.message);setTimeout(streamEvents,3000);}
}

function handleEvent(ev,d){
  if(ev==="snapshot"){
    state.routing=d.routing;state.accounts=d.accounts||[];
    (d.active_requests||[]).forEach(r=>state.active[r.id]=r);
    state.completed=d.completed_requests||[];
    writeRoutingForm(state.routing);if(d.throttle)writeThrottleForm(d.throttle);renderAll();
  }else if(ev==="request_started"){state.active[d.id]=d;renderActive();}
   else if(ev==="attempt"){if(state.active[d.id]){state.active[d.id]=d;renderActive();}}
   else if(ev==="request_finished"){delete state.active[d.id];state.expandedActive.delete(d.id);state.completed.unshift(d);if(state.completed.length>200)state.completed.pop();renderActive();renderHistory();}
   else if(ev==="stream_progress"){
     const r=state.active[d.id];if(r){
       if(d.ttft_s!=null)r.ttft_s=d.ttft_s;if(d.tps!=null)r.tps=d.tps;
       if(d.output_tokens!=null)r.output_tokens=d.output_tokens;
       if(d.content_delta){r.chunks=r.chunks||[];r.chunks.push(d.content_delta);if(r.chunks.length>80)r.chunks=r.chunks.slice(-80);}
       // In-place update: find the row and patch cells without rebuilding
       const tr=document.querySelector(`#activeWrap tr[data-id="${d.id}"]`);
       if(tr){
         const cells=tr.querySelectorAll("td");
         if(cells.length>=7){
           cells[4].textContent=r.ttft_s!=null?r.ttft_s.toFixed(2)+"s":"…";
           cells[5].textContent=r.tps!=null?r.tps.toFixed(1)+" t/s":"…";
           cells[6].textContent=r.output_tokens??"…";
         }
         // Update expanded detail content if open
         const exp=tr.nextElementSibling;
         if(exp&&exp.classList.contains("exp")){
           const pre=exp.querySelector("pre.code");
           if(pre&&d.content_delta){pre.textContent+=d.content_delta;pre.scrollTop=pre.scrollHeight;}
         }
       }else{renderActive();}
     }
   }
   else if(ev==="log"){pushLog(d);}
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
function renderAll(){renderStripe();renderAccounts();renderActive();renderHistory();}

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
    return `<div class="acct ${cur}">
      <div class="name"><span>${esc(a.display_id)}</span><span class="tag ${a.is_current?'on':''} ${a.failures>0?'err':''}">${a.is_current?'使用中':(a.failures>0?'冷卻中':'待命')}</span></div>
      <dl><dt>總計</dt><dt>成功</dt><dt>失敗</dt>
        <dd>${a.stats.total_requests}</dd><dd>${a.stats.successful_requests}</dd><dd>${a.stats.failed_requests}</dd></dl>
      ${cool}${lastErr}</div>`;
  }).join("");
}

function reqRow(r,isActive){
  const statusCls=r.status==="completed"?"ok":(r.status==="active"?"active":(r.status==="client_disconnected"?"dis":"err"));
  const ttft=r.ttft_s!=null?fmtMs(r.ttft_s):"—";const tps=r.tps!=null?`${r.tps.toFixed(1)}t/s`:"—";
  const trim=r.trim_before_messages?`${r.trim_before_messages}→${r.trim_after_messages} msg`:"";
  const attempts=(r.attempts||[]).length;
  return `<tr class="row" data-id="${esc(r.id)}"><td>${fmtTime(r.started_at)}</td>
    <td><span class="statusbadge ${statusCls}">${esc(r.status)}</span></td>
    <td>${esc(r.api_format)} ${r.stream?"⋯":""}</td>
    <td>${esc(r.original_model)}${r.original_model!==r.active_model?`→${esc(r.active_model)}`:""}</td>
    <td>${ttft}</td><td>${tps}</td><td>${r.output_tokens??"—"}</td><td>${attempts}</td><td>${esc(trim)}</td>
    <td>${r.error?`<span class="statusbadge err">${esc(r.error.slice(0,60))}</span>`:""}</td></tr>`;
}

function renderActive(){
  const rows=Object.values(state.active).sort((a,b)=>b.started_at-a.started_at);
  $("#activeCount").textContent=rows.length;
  const el=$("#activeWrap");
  if(!rows.length){el.innerHTML=`<p style="color:var(--muted)">目前沒有進行中的請求。</p>`;return;}
  el.innerHTML=`<table class="reqs"><thead><tr><th>開始</th><th>狀態</th><th>格式</th><th>模型</th><th>首字</th><th>速度</th><th>token</th><th>重試</th><th>裁剪</th><th>錯誤</th></tr></thead>
    <tbody>${rows.map(r=>reqRow(r,true)).join("")}</tbody></table>`;
  wireRowExpand("#activeWrap","active");
}

function renderHistory(){
  const f=histFilterText.toLowerCase().trim();
  const rows=state.completed.filter(r=>{
    if(!f)return true;
    return JSON.stringify(r).toLowerCase().includes(f);
  });
  const el=$("#historyWrap");
  if(!rows.length){el.innerHTML=`<p style="color:var(--muted)">尚無已完成的請求。</p>`;return;}
  el.innerHTML=`<table class="reqs"><thead><tr><th>開始</th><th>狀態</th><th>格式</th><th>模型</th><th>首字</th><th>速度</th><th>token</th><th>重試</th><th>裁剪</th><th>錯誤</th></tr></thead>
    <tbody>${rows.slice(0,300).map(r=>reqRow(r,false)).join("")}</tbody></table>`;
  wireRowExpand("#historyWrap","completed");
}

$("#histFilter").addEventListener("input",e=>{histFilterText=e.target.value;renderHistory();});

function wireRowExpand(wrapSel,kind){
  const expandedSet=(kind==="active"?state.expandedActive:state.expandedCompleted);
  // Restore expanded rows after table rebuild
  $(wrapSel).querySelectorAll("tr.row").forEach(tr=>{
    const id=tr.dataset.id;
    if(expandedSet.has(id)){
      const r=(kind==="active"?state.active[id]:state.completed.find(x=>x.id===id));
      if(r){
        const exp=document.createElement("tr");exp.className="exp";exp.dataset.expFor=id;
        exp.innerHTML=`<td colspan="10">${renderDetail(r)}</td>`;
        tr.after(exp);
      }
    }
    tr.addEventListener("click",()=>{
      const next=tr.nextElementSibling;
      if(next&&next.classList.contains("exp")){next.remove();expandedSet.delete(id);return;}
      const r=(kind==="active"?state.active[id]:state.completed.find(x=>x.id===id));
      if(!r)return;
      const exp=document.createElement("tr");exp.className="exp";exp.dataset.expFor=id;
      exp.innerHTML=`<td colspan="10">${renderDetail(r)}</td>`;
      tr.after(exp);
      expandedSet.add(id);
    });
  });
}

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
    chunks=`<div style="margin-top:4px"><strong>串流內容</strong> <span style="color:var(--muted);font-size:11px">(即時更新)</span></div><pre class="code" style="max-height:300px;overflow:auto">${esc(chunkText)}</pre>`;
  }else if(!isActive&&(r.chunks||[]).length){
    chunks=`<details class="payload"><summary>stream chunks (${r.chunks.length})</summary><pre class="code">${esc(chunkText)}</pre></details>`;
  }
  const resp=r.response?`<details class="payload"><summary>response</summary><pre class="code">${esc(r.response)}</pre></details>`:"";
  return `<div style="padding:6px 4px"><div style="color:var(--muted);margin-bottom:4px">id=${esc(r.id)} · reason=${esc(r.routing_reason||"")}</div>
    <div style="margin-bottom:4px"><strong>嘗試紀錄</strong>${attempts||" —"}</div>${payloads}${chunks}${resp}</div>`;
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

// -------- routing form (unchanged behavior) --------
function writeRoutingForm(r){if(!r)return;
  $("#enabled").checked=r.enabled;$("#mode").value=r.mode;$("#manualModel").value=r.manual_model;
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
async function quickSwap(m){$("#enabled").checked=true;$("#mode").value="manual";$("#manualModel").value=m;await applyRouting();}
async function resetRouting(){await api("/dashboard/api/routing/reset",{method:"POST",body:"{}"});$("#saveResult").textContent="已重設";}
async function clearMonitor(){await api("/dashboard/api/monitor/clear",{method:"POST",body:"{}"});state.completed=[];renderHistory();}
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

if(hasKey())connect();else setConn(false,"未輸入金鑰");

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
</script>
</body></html>
"""
