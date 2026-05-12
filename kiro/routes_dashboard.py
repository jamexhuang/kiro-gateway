# -*- coding: utf-8 -*-

"""
Web dashboard for runtime model routing and request monitoring.

The dashboard is intentionally self-contained: no asset build, no external CDN,
no startup hooks, and no background tasks.
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Security, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field

from kiro.config import PROXY_API_KEY, APP_VERSION
from kiro.control_panel import RoutingConfig, control_panel


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


@router.post("/dashboard/api/monitor/clear", dependencies=[Security(verify_dashboard_api_key)])
async def clear_dashboard_monitor() -> Dict[str, str]:
    """
    Clear completed monitoring history.

    Returns:
        Operation status.
    """
    control_panel.clear_monitor()
    return {"status": "ok"}


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
  <title>Kiro Gateway 控制面板</title>
  <style>
    :root {
      --ink: #18211f;
      --muted: #65706c;
      --paper: #f5f1e8;
      --card: rgba(255, 253, 247, .86);
      --line: rgba(24, 33, 31, .13);
      --accent: #c45f2c;
      --accent-2: #1d6f68;
      --danger: #9b2d20;
      --shadow: 0 24px 70px rgba(43, 35, 24, .16);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 12%, rgba(196, 95, 44, .22), transparent 34rem),
        radial-gradient(circle at 94% 4%, rgba(29, 111, 104, .22), transparent 30rem),
        linear-gradient(135deg, #f3eadb 0%, #eef0e7 52%, #e1eadf 100%);
      font-family: "Avenir Next", "IBM Plex Sans", "Gill Sans", sans-serif;
      min-height: 100vh;
    }

    main {
      width: min(1440px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 42px;
    }

    header {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-end;
      margin-bottom: 22px;
    }

    h1 {
      font-family: "Iowan Old Style", "Palatino", Georgia, serif;
      font-size: clamp(34px, 5vw, 72px);
      line-height: .92;
      letter-spacing: -.05em;
      margin: 0;
      max-width: 760px;
    }

    .subtitle {
      color: var(--muted);
      margin-top: 12px;
      font-size: 15px;
      max-width: 760px;
    }

    .grid {
      display: grid;
      grid-template-columns: 440px 1fr;
      gap: 18px;
      align-items: start;
    }

    .intro-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
      margin-bottom: 18px;
    }

    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 26px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      padding: 20px;
    }

    .intro-card {
      min-height: 150px;
      animation: rise .42s ease both;
    }

    .intro-card:nth-child(2) { animation-delay: .06s; }
    .intro-card:nth-child(3) { animation-delay: .12s; }

    @keyframes rise {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .card h2 {
      margin: 0 0 14px;
      font-size: 14px;
      letter-spacing: .16em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .card h3 {
      margin: 0 0 8px;
      font-size: 18px;
    }

    .card p {
      margin: 8px 0;
      line-height: 1.58;
    }

    label {
      display: block;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
      margin: 14px 0 7px;
    }

    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .68);
      color: var(--ink);
      border-radius: 15px;
      padding: 12px 13px;
      font: inherit;
      outline: none;
    }

    textarea {
      min-height: 92px;
      resize: vertical;
      font-family: "SF Mono", "Cascadia Mono", Menlo, monospace;
      font-size: 12px;
    }

    button {
      border: 0;
      border-radius: 999px;
      padding: 11px 15px;
      font-weight: 800;
      color: #fffaf0;
      background: var(--ink);
      cursor: pointer;
      transition: transform .15s ease, opacity .15s ease;
    }

    button:hover { transform: translateY(-1px); }
    button.secondary { background: var(--accent-2); }
    button.danger { background: var(--danger); }
    button.ghost {
      background: transparent;
      color: var(--ink);
      border: 1px solid var(--line);
    }

    .row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }

    .switch-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 11px 0;
      border-bottom: 1px solid var(--line);
    }

    .switch-row input { width: auto; transform: scale(1.2); }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .52);
      padding: 8px 11px;
      font-size: 12px;
      color: var(--muted);
    }

    .dot {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: var(--accent-2);
      box-shadow: 0 0 0 5px rgba(29, 111, 104, .13);
    }

    .request {
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px;
      background: rgba(255, 255, 255, .48);
      margin-bottom: 12px;
    }

    .request-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      font-family: "SF Mono", "Cascadia Mono", Menlo, monospace;
      font-size: 12px;
    }

    .models {
      margin-top: 8px;
      font-size: 13px;
    }

    pre {
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 360px;
      overflow: auto;
      background: #18211f;
      color: #fff4dd;
      border-radius: 14px;
      padding: 13px;
      font-family: "SF Mono", "Cascadia Mono", Menlo, monospace;
      font-size: 11px;
      line-height: 1.45;
    }

    details { margin-top: 10px; }
    summary { cursor: pointer; color: var(--accent); font-weight: 800; }
    .muted { color: var(--muted); }
    .error { color: var(--danger); font-weight: 800; }
    .success { color: var(--accent-2); font-weight: 800; }

    .notice {
      border: 1px solid rgba(29, 111, 104, .22);
      background: rgba(29, 111, 104, .08);
      border-radius: 18px;
      padding: 12px;
      color: var(--ink);
      margin: 12px 0;
      line-height: 1.55;
    }

    .result {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, .54);
      border-radius: 16px;
      padding: 12px;
      margin-top: 12px;
      min-height: 48px;
      line-height: 1.55;
    }

    .kbd {
      font-family: "SF Mono", "Cascadia Mono", Menlo, monospace;
      font-size: 12px;
      background: rgba(24, 33, 31, .08);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 2px 6px;
    }

    .account-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }
    .account-card {
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 16px;
      background: rgba(255, 255, 255, .48);
      transition: all .2s ease;
    }
    .account-card.current {
      border-color: var(--accent-2);
      background: rgba(29, 111, 104, .05);
      box-shadow: 0 4px 12px rgba(29, 111, 104, .08);
    }
    .account-card h4 {
      margin: 0 0 10px;
      font-size: 14px;
      word-break: break-all;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .account-card .tag {
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 6px;
      background: var(--line);
      color: var(--muted);
      text-transform: uppercase;
    }
    .account-card.current .tag {
      background: var(--accent-2);
      color: white;
    }
    .account-stats {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 8px;
      font-size: 11px;
      text-align: center;
    }
    .stat-item {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .stat-val { font-weight: 800; font-size: 13px; }

    @media (max-width: 980px) {
      header { display: block; }
      .grid { grid-template-columns: 1fr; }
      .intro-grid { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>模型路由控制台 <small style="font-size: 14px; opacity: 0.5;">vAPP_VERSION_PLACEHOLDER</small></h1>
        <div class="subtitle">
          即時切換 Kiro Gateway 使用的模型，不需要改 client 設定，也不需要重啟服務。
          所有設定只存在目前伺服器記憶體；重啟後會回到安全預設。內容監控預設關閉，避免不必要地保存敏感資料。
        </div>
      </div>
      <div class="pill"><span class="dot"></span><span id="status">尚未輸入 API Key</span></div>
    </header>

    <section class="intro-grid">
      <article class="card intro-card">
        <h2>功能說明</h2>
        <h3>不改 client，直接改實際上游模型</h3>
        <p class="muted">
          Client 仍然可以送 <span class="kbd">claude-opus-4-7</span>。
          控制台可以在 gateway 內即時改成 4.6、4.7，或依照模型名稱做 redirect。
        </p>
      </article>
      <article class="card intro-card">
        <h2>效果說明</h2>
        <h3>Manual、Redirect、Fallback 的差異</h3>
        <p class="muted">
          Manual 會強制所有請求走指定模型；Redirect 只改命中的模型；
          Fallback 只適合 4.7 → 4.6 這種同等級 Opus 內切換；不要把 Opus 自動降到 Sonnet。
          如果只想換帳號，請關閉模型 Fallback，交給 account manager 處理。
        </p>
      </article>
      <article class="card intro-card">
        <h2>監控安全</h2>
        <h3>看得到真實模型與請求狀態</h3>
        <p class="muted">
          Active 顯示正在傳遞的請求，Completed 顯示近期完成紀錄。
          只有打開「擷取內容」才會把 payload/stream 片段保存在記憶體中。
        </p>
      </article>
    </section>

    <section class="grid">
      <aside class="card">
        <h2>存取驗證</h2>
        <label for="apiKey">Proxy API Key</label>
        <input id="apiKey" type="password" placeholder="和 client 使用的 Bearer key 相同">
        <div class="notice">
          未輸入 Key 時，頁面不會輪詢 API，因此不會再洗出 401。Key 只存在這個瀏覽器的 localStorage。
        </div>
        <div class="actions">
          <button onclick="saveKey()">儲存並連線</button>
          <button class="ghost" onclick="forgetKey()">清除 Key</button>
        </div>

        <h2 style="margin-top:26px">路由設定</h2>
        <div class="switch-row">
          <div>
            <strong>啟用即時路由</strong>
            <div class="muted">關閉時完全維持原本專案行為。</div>
          </div>
          <input id="enabled" type="checkbox">
        </div>
        <div class="switch-row">
          <div>
            <strong>失敗時回原模型</strong>
            <div class="muted">改寫失敗後先重試 client 原本要求的模型。</div>
          </div>
          <input id="safeFallback" type="checkbox">
        </div>
        <div class="switch-row">
          <div>
            <strong>啟用模型 Fallback</strong>
            <div class="muted">關閉時只會換帳號，不會自動降級到其他模型家族。</div>
          </div>
          <input id="fallbackEnabled" type="checkbox">
        </div>
        <div class="switch-row">
          <div>
            <strong>擷取傳遞內容</strong>
            <div class="muted">只存在記憶體；建議排錯時短暫開啟。</div>
          </div>
          <input id="captureContent" type="checkbox">
        </div>

        <label for="mode">模式</label>
        <select id="mode">
          <option value="passthrough">Passthrough：不改模型</option>
          <option value="manual">Manual：所有請求強制指定模型</option>
          <option value="redirect">Redirect：依模型名稱重新導向</option>
        </select>

        <label for="manualModel">Manual 指定模型</label>
        <input id="manualModel" placeholder="claude-opus-4.6">

        <label for="redirects">Redirect 規則 JSON</label>
        <textarea id="redirects" spellcheck="false"></textarea>

        <label for="fallbackModels">同等級模型 Fallback，逗號分隔</label>
        <input id="fallbackModels" placeholder="例如 claude-opus-4.6；不要填 claude-sonnet">
        <div class="notice">
          Opus 失敗時自動降到 Sonnet 會改變輸出品質與能力。控制台會避免 Opus → Sonnet/Haiku 這類降級；429 仍會由既有帳號系統嘗試其他帳號。
        </div>

        <div class="actions">
          <button onclick="applyRouting()">套用路由</button>
          <button class="secondary" onclick="quickSwap('claude-opus-4.6')">強制 4.6</button>
          <button class="secondary" onclick="quickSwap('claude-opus-4.7')">強制 4.7</button>
          <button class="danger" onclick="resetRouting()">回安全預設</button>
        </div>
        <div id="saveResult" class="result muted">尚未套用新的設定。</div>

        <h2 style="margin-top:26px">變更測試</h2>
        <p class="muted">這個測試只用目前 runtime 設定試算路由結果，不會送到 Kiro，也不消耗模型額度。</p>
        <label for="testModel">測試 client 送出的模型</label>
        <input id="testModel" value="claude-opus-4-7">
        <div class="actions">
          <button class="ghost" onclick="runRoutingTest()">測試目前設定</button>
        </div>
        <div id="testResult" class="result muted">套用後會在這裡顯示「原模型 → 實際上游模型」。</div>
      </aside>

      <section class="card">
        <div style="display:flex;justify-content:space-between;gap:12px;align-items:center">
          <h2>帳號系統狀態</h2>
          <div class="pill"><span id="accountCount">0 帳號</span></div>
        </div>
        <div id="accounts" class="account-list">
          <p class='muted'>載入中...</p>
        </div>

        <div style="display:flex;justify-content:space-between;gap:12px;align-items:center">
          <h2>請求監控</h2>
          <div class="actions" style="margin:0">
            <button class="ghost" onclick="refresh(true)">重新整理</button>
            <button class="ghost" onclick="clearMonitor()">清除完成紀錄</button>
          </div>
        </div>
        <div class="row">
          <div>
            <h2>進行中</h2>
            <div id="active"></div>
          </div>
          <div>
            <h2>已完成</h2>
            <div id="completed"></div>
          </div>
        </div>
      </section>
    </section>
  </main>

  <script>
    const keyInput = document.querySelector("#apiKey");
    const statusEl = document.querySelector("#status");
    const saveResult = document.querySelector("#saveResult");
    const testResult = document.querySelector("#testResult");
    
    // Auto-bring cred from server injection
    const autoKey = window.KIRO_AUTO_KEY;
    let storedKey = localStorage.getItem("kiro-dashboard-key");
    
    console.log("Kiro Dashboard Init: autoKey=" + (autoKey ? "found" : "not found") + ", storedKey=" + (storedKey ? "found" : "empty"));
    
    if (autoKey) {
        if (!storedKey || storedKey.length < 3) {
            console.log("Kiro Dashboard: Applying auto-fill key to empty/short storage");
            localStorage.setItem("kiro-dashboard-key", autoKey);
            storedKey = autoKey;
        }
    }
    
    keyInput.value = storedKey || "";
    
    let authPaused = false;

    keyInput.addEventListener("input", () => {
      authPaused = false;
    });

    function hasApiKey() {
      return keyInput.value.trim().length > 0;
    }

    function authHeaders() {
      return {
        "Authorization": `Bearer ${keyInput.value.trim()}`,
        "Content-Type": "application/json"
      };
    }

    function saveKey() {
      if (!hasApiKey()) {
        setStatus("尚未輸入 API Key，不會輪詢 API");
        renderUnauthenticated();
        return;
      }
      authPaused = false;
      localStorage.setItem("kiro-dashboard-key", keyInput.value.trim());
      setStatus("API Key 已儲存，正在讀取狀態");
      refresh(true);
    }

    function forgetKey() {
      localStorage.removeItem("kiro-dashboard-key");
      keyInput.value = "";
      authPaused = false;
      setStatus("尚未輸入 API Key，不會輪詢 API");
      renderUnauthenticated();
    }

    async function api(path, options = {}) {
      if (!hasApiKey()) {
        throw new Error("請先輸入 Proxy API Key。");
      }
      const res = await fetch(path, {
        ...options,
        headers: { ...authHeaders(), ...(options.headers || {}) }
      });
      if (res.status === 401) {
        throw new Error("API Key 錯誤或未授權。");
      }
      if (!res.ok) {
        throw new Error(await res.text());
      }
      return res.json();
    }

    function setStatus(message) {
      statusEl.textContent = message;
    }

    function setSaveResult(message, kind = "success") {
      saveResult.className = `result ${kind}`;
      saveResult.textContent = message;
    }

    function renderUnauthenticated() {
      document.querySelector("#active").innerHTML =
        "<p class='muted'>輸入 API Key 後才會讀取進行中的請求。</p>";
      document.querySelector("#completed").innerHTML =
        "<p class='muted'>輸入 API Key 後才會讀取完成紀錄。</p>";
      document.querySelector("#accounts").innerHTML =
        "<p class='muted'>輸入 API Key 後才會讀取帳號狀態。</p>";
    }

    function readRoutingForm() {
      let redirects = {};
      try {
        redirects = JSON.parse(document.querySelector("#redirects").value || "{}");
      } catch (err) {
        throw new Error("Redirect 規則必須是合法 JSON。");
      }
      return {
        enabled: document.querySelector("#enabled").checked,
        mode: document.querySelector("#mode").value,
        manual_model: document.querySelector("#manualModel").value,
        redirects,
        fallback_enabled: document.querySelector("#fallbackEnabled").checked,
        fallback_models: document.querySelector("#fallbackModels").value
          .split(",").map(v => v.trim()).filter(Boolean),
        safe_fallback_to_original: document.querySelector("#safeFallback").checked,
        capture_content: document.querySelector("#captureContent").checked
      };
    }

    function writeRoutingForm(routing) {
      document.querySelector("#enabled").checked = routing.enabled;
      document.querySelector("#mode").value = routing.mode;
      document.querySelector("#manualModel").value = routing.manual_model;
      document.querySelector("#redirects").value = JSON.stringify(routing.redirects, null, 2);
      document.querySelector("#fallbackModels").value = routing.fallback_models.join(", ");
      document.querySelector("#fallbackEnabled").checked = routing.fallback_enabled;
      document.querySelector("#safeFallback").checked = routing.safe_fallback_to_original;
      document.querySelector("#captureContent").checked = routing.capture_content;
    }

    async function applyRouting() {
      try {
        const data = await api("/dashboard/api/routing", {
          method: "PUT",
          body: JSON.stringify(readRoutingForm())
        });
        writeRoutingForm(data.routing);
        setSaveResult(`設定已套用成功：${data.routing.mode} / ${data.routing.manual_model}`);
        await runRoutingTest(true);
        refresh(true);
      } catch (err) {
        setSaveResult(err.message, "error");
      }
    }

    async function quickSwap(model) {
      document.querySelector("#enabled").checked = true;
      document.querySelector("#mode").value = "manual";
      document.querySelector("#manualModel").value = model;
      await applyRouting();
    }

    async function resetRouting() {
      await api("/dashboard/api/routing/reset", { method: "POST", body: "{}" });
      setSaveResult("已回到安全預設：不改寫模型、內容擷取關閉。");
      await runRoutingTest(true);
      refresh(true);
    }

    async function clearMonitor() {
      await api("/dashboard/api/monitor/clear", { method: "POST", body: "{}" });
      refresh(true);
    }

    async function runRoutingTest(silent = false) {
      try {
        const model = document.querySelector("#testModel").value.trim() || "claude-opus-4-7";
        const data = await api("/dashboard/api/routing/test", {
          method: "POST",
          body: JSON.stringify({ model })
        });
        const decision = data.decision;
        const fallbacks = decision.fallback_models.length
          ? `；Fallback：${decision.fallback_models.join(", ")}`
          : "；沒有 Fallback";
        testResult.className = "result success";
        testResult.innerHTML =
          `測試成功：<span class="kbd">${escapeHtml(decision.original_model)}</span> → ` +
          `<span class="kbd">${escapeHtml(decision.routed_model)}</span>` +
          `<br>${escapeHtml(decision.reason)}${escapeHtml(fallbacks)}`;
        if (!silent) {
          setStatus("路由測試成功");
        }
      } catch (err) {
        testResult.className = "result error";
        testResult.textContent = err.message;
        if (!silent) {
          setStatus("路由測試失敗");
        }
      }
    }

    function fmtTime(ts) {
      if (!ts) return "";
      return new Date(ts * 1000).toLocaleTimeString();
    }

    function renderRecord(record) {
      const payloads = Object.entries(record.payloads || {})
        .map(([name, body]) => `<details><summary>${escapeHtml(name)}</summary><pre>${escapeHtml(body)}</pre></details>`)
        .join("");
      const chunks = (record.chunks || []).length
        ? `<details><summary>stream chunks (${record.chunks.length})</summary><pre>${escapeHtml(record.chunks.join("\n\n"))}</pre></details>`
        : "";
      const response = record.response
        ? `<details><summary>response</summary><pre>${escapeHtml(record.response)}</pre></details>`
        : "";
      const attempts = (record.attempts || [])
        .map(a => `${escapeHtml(a.model)} ${a.http_status || ""} ${a.status}`)
        .join(" | ");
      return `<div class="request">
        <div class="request-head">
          <div>${escapeHtml(record.id)} · ${escapeHtml(record.api_format)} · ${record.stream ? "串流" : "非串流"}</div>
          <div>${fmtTime(record.started_at)}</div>
        </div>
        <div class="models"><strong>${escapeHtml(record.original_model)}</strong> → <strong>${escapeHtml(record.active_model)}</strong></div>
        <div class="muted">${escapeHtml(record.routing_reason || "")}</div>
        <div class="muted">${escapeHtml(attempts)}</div>
        ${record.error ? `<div class="error">${escapeHtml(record.error)}</div>` : ""}
        ${payloads}${chunks}${response}
      </div>`;
    }

    function renderAccount(account) {
      const isCurrent = account.is_current ? "current" : "";
      const statusClass = account.failures > 0 ? "error" : "success";
      
      return `
        <div class="account-card ${isCurrent}">
          <h4>
            <span>${escapeHtml(account.display_id)}</span>
            <span class="tag">${account.is_current ? "使用中" : "備用"}</span>
          </h4>
          <div class="account-stats">
            <div class="stat-item"><span class="muted">總次數</span><span class="stat-val">${account.stats.total_requests}</span></div>
            <div class="stat-item"><span class="success">成功</span><span class="stat-val">${account.stats.successful_requests}</span></div>
            <div class="stat-item"><span class="error">失敗</span><span class="stat-val">${account.stats.failed_requests}</span></div>
          </div>
          <div class="account-status">
            <span class="${statusClass}">${account.failures > 0 ? account.failures + ' 次失敗' : '運作正常'}</span>
            <span class="muted">${fmtTime(account.models_cached_at)} 更新</span>
          </div>
        </div>
      `;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    async function refresh(force = false) {
      try {
        const state = await api("/dashboard/api/state");
        if (!state.authenticated) {
          authPaused = true;
          setStatus("API Key 尚未驗證，已停止自動輪詢");
          renderUnauthenticated();
          return;
        }
        writeRoutingForm(state.routing);

        // Render Accounts
        const accountsEl = document.querySelector("#accounts");
        if (state.accounts && state.accounts.length) {
          accountsEl.innerHTML = state.accounts.map(renderAccount).join("");
          document.querySelector("#accountCount").textContent = `${state.accounts.length} 帳號`;
        } else {
          accountsEl.innerHTML = "<p class='muted'>沒有帳號資訊。</p>";
        }

        document.querySelector("#active").innerHTML =
          state.active_requests.length ? state.active_requests.map(renderRecord).join("") : "<p class='muted'>目前沒有進行中的請求。</p>";
        document.querySelector("#completed").innerHTML =
          state.completed_requests.length ? state.completed_requests.map(renderRecord).join("") : "<p class='muted'>目前沒有完成紀錄。</p>";
        setStatus(`${state.active_requests.length} 進行中 · ${state.completed_requests.length} 已完成`);
      } catch (err) {
        authPaused = true;
        setStatus(err.message);
      }
    }

    renderUnauthenticated();
    if (hasApiKey()) {
      refresh(true);
    }
    setInterval(() => {
      if (hasApiKey() && !authPaused) {
        refresh(false);
      }
    }, 1200);
  </script>
</body>
</html>"""
