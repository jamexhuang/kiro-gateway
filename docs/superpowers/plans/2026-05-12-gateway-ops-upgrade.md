# Kiro Gateway Ops Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Kiro Gateway observable and professional: quiet logs, fast failover on capacity errors, mid-stream retry on upstream disconnects, SSE-based dashboard, in-browser live log console, per-request performance metrics, account cooldown visibility, virtual-scrolled history, and rolling RPS/latency/error sparklines.

**Architecture:**
- Extend the existing `control_panel` in-memory store with performance metrics and a ring-buffer log sink.
- Replace the 1.2 s polling `/dashboard/api/state` with an SSE `/dashboard/api/events` stream (initial snapshot + incremental diffs). Keep `/state` as fallback.
- Quick-fail `INSUFFICIENT_MODEL_CAPACITY` (429) and `RemoteProtocolError` in `http_client` / `streaming_core` so failover happens in < 1 s instead of 3+ s.
- Rebuild dashboard HTML as a sidebar-driven single-page app (still light theme per the user's preference — no dark ops mode), with dedicated panels for Accounts, Active/Completed requests, Live Log, and Metrics.
- Deploy: short restart of `python main.py --host 0.0.0.0 --port 8000` (currently PID 78173, PPID 24802 — a terminal/tmux session). Brief downtime is acceptable per the user.

**Tech Stack:** Python 3.10, FastAPI, httpx, loguru, uvicorn, vanilla JS + EventSource (no build step).

---

## File Structure

**Modify:**
- `kiro/control_panel.py` — add `TTFT/TPS/tokens/trimmed/input_size/output_size` on `RequestRecord`; add `LogRingBuffer`, `MetricsBucket`, and an event-subscriber set for SSE.
- `kiro/http_client.py` — fast-fail 429 when reason=`INSUFFICIENT_MODEL_CAPACITY`; classify `RemoteProtocolError` as retryable with shorter backoff for mid-body disconnects.
- `kiro/streaming_core.py` — treat `RemoteProtocolError` raised before/early-stream as retryable via the existing first-token retry loop.
- `kiro/streaming_anthropic.py` / `kiro/streaming_openai.py` — after `logger.info(... TTFT=...)`, call `control_panel.record_metrics(request_id, ttft, tps, tokens, ...)`.
- `kiro/converters_core.py` — after trim-log, call `control_panel.record_trim(request_id, before, after, before_bytes, after_bytes)`.
- `kiro/account_manager.py` — `get_accounts_snapshot()` must include `cooldown_remaining_s`, `cooldown_total_s`, `backoff_tier`, `last_error_reason`, `last_error_status`, `current_model_cache`.
- `kiro/routes_anthropic.py` / `kiro/routes_openai.py` — pass `request_id` into tokenizer path so metrics hook knows which record to update.
- `kiro/routes_dashboard.py` — add `/dashboard/api/events` (SSE), `/dashboard/api/logs` (ring-buffer dump), `/dashboard/api/metrics` (sparkline series). Suppress noise by filtering `/dashboard/api/*` out of uvicorn access logs.
- `main.py` — install loguru ring-buffer sink; add uvicorn access log filter.
- Rewrite `DASHBOARD_HTML` inside `kiro/routes_dashboard.py` (sidebar layout, SSE client, virtual scroll, live log, sparklines).

**Create:**
- `kiro/log_buffer.py` — `LogRingBuffer` loguru sink (thread-safe deque + subscriber fanout).
- `kiro/metrics.py` — rolling 5-min buckets for RPS / P50 / P95 / error rate.
- `tests/unit/test_log_buffer.py`
- `tests/unit/test_metrics.py`
- `tests/unit/test_sse_events.py`

**No split** of the large existing files — the dashboard HTML stays embedded in `routes_dashboard.py` to match current project convention (self-contained, no asset build).

---

## Task 1: Silence `/dashboard/api/*` uvicorn access logs

**Files:**
- Modify: `main.py:104-179` (InterceptHandler class and `setup_logging_intercept`)
- Test: `tests/unit/test_main_cli.py` (add new test case)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_main_cli.py`:

```python
def test_intercept_handler_drops_dashboard_access_logs(caplog):
    import logging
    from main import InterceptHandler

    handler = InterceptHandler()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1:51901 - "GET /dashboard/api/state HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    captured = []
    from loguru import logger as loguru_logger
    sink_id = loguru_logger.add(lambda msg: captured.append(msg), level="INFO")
    try:
        handler.emit(record)
    finally:
        loguru_logger.remove(sink_id)
    assert captured == [], "dashboard access logs must be dropped"

def test_intercept_handler_keeps_v1_messages_access_logs():
    import logging
    from main import InterceptHandler

    handler = InterceptHandler()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1:51901 - "POST /v1/messages HTTP/1.1" 200',
        args=(),
        exc_info=None,
    )
    captured = []
    from loguru import logger as loguru_logger
    sink_id = loguru_logger.add(lambda msg: captured.append(msg), level="INFO")
    try:
        handler.emit(record)
    finally:
        loguru_logger.remove(sink_id)
    assert len(captured) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_main_cli.py::test_intercept_handler_drops_dashboard_access_logs -v`
Expected: FAIL (dashboard logs currently pass through).

- [ ] **Step 3: Add filter to InterceptHandler**

In `main.py`, add a class attribute and check at the top of `emit`:

```python
class InterceptHandler(logging.Handler):
    SHUTDOWN_EXCEPTIONS = (
        "CancelledError",
        "KeyboardInterrupt",
        "asyncio.exceptions.CancelledError",
    )

    ACCESS_LOG_DROP_PATTERNS = (
        "/dashboard/api/state",
        "/dashboard/api/events",
        "/dashboard/api/logs",
        "/dashboard/api/metrics",
    )

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uvicorn.access":
            msg = record.getMessage()
            if any(pat in msg for pat in self.ACCESS_LOG_DROP_PATTERNS):
                return

        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and exc_type.__name__ in self.SHUTDOWN_EXCEPTIONS:
                logger.info("Server shutdown in progress...")
                return

        msg = record.getMessage()
        if any(exc in msg for exc in self.SHUTDOWN_EXCEPTIONS):
            return

        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
```

- [ ] **Step 4: Run both tests**

Run: `pytest tests/unit/test_main_cli.py -k intercept_handler -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/unit/test_main_cli.py
git commit -m "feat(log): drop /dashboard/api/* access logs to keep console readable"
```

---

## Task 2: Fast-fail 429 `INSUFFICIENT_MODEL_CAPACITY` for immediate failover

**Files:**
- Modify: `kiro/http_client.py:251-259`
- Test: `tests/unit/test_http_client.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_http_client.py`:

```python
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock
from kiro.http_client import KiroHttpClient

@pytest.mark.asyncio
async def test_429_insufficient_capacity_returns_immediately_without_retry():
    auth_manager = MagicMock()
    auth_manager.get_access_token = AsyncMock(return_value="tok")
    auth_manager.api_host = "https://x"

    response_429 = MagicMock(spec=httpx.Response)
    response_429.status_code = 429
    response_429.aread = AsyncMock(
        return_value=b'{"reason": "INSUFFICIENT_MODEL_CAPACITY"}'
    )

    client = KiroHttpClient(auth_manager, shared_client=MagicMock())
    client.client = MagicMock()
    client.client.request = AsyncMock(return_value=response_429)

    import time
    t0 = time.time()
    result = await client.request_with_retry("POST", "https://x/foo", {"a": 1})
    elapsed = time.time() - t0

    assert result.status_code == 429
    assert elapsed < 0.5, f"Fast-fail expected, took {elapsed:.2f}s"
    assert client.client.request.await_count == 1, "must not retry on INSUFFICIENT_MODEL_CAPACITY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_http_client.py::test_429_insufficient_capacity_returns_immediately_without_retry -v`
Expected: FAIL (currently waits 1 s + 2 s and tries 3 times).

- [ ] **Step 3: Peek at body before backoff**

Replace the 429 branch in `kiro/http_client.py` `request_with_retry`:

```python
                # 429 - rate limit, wait and retry
                if response.status_code == 429:
                    last_response = response
                    fast_fail = False
                    if not stream:
                        try:
                            body_bytes = await response.aread()
                            import json as _json
                            payload = _json.loads(body_bytes.decode("utf-8", errors="replace"))
                            reason = payload.get("reason") or payload.get("error", {}).get("reason")
                            if reason in ("INSUFFICIENT_MODEL_CAPACITY", "THROTTLING"):
                                logger.warning(
                                    f"429 {reason}: skipping retry, letting caller failover"
                                )
                                fast_fail = True
                                response._content = body_bytes
                        except Exception:
                            pass
                    if fast_fail or attempt >= max_retries - 1:
                        break
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Received 429, waiting {delay}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                    continue
```

Note: for `stream=True` we can't read the body without consuming the stream; leave stream 429s on the existing retry path (the failover loop in routes already handles those).

- [ ] **Step 4: Run the test**

Run: `pytest tests/unit/test_http_client.py::test_429_insufficient_capacity_returns_immediately_without_retry -v`
Expected: PASS.

- [ ] **Step 5: Confirm existing 429 retry behavior still works**

Run: `pytest tests/unit/test_http_client.py -v`
Expected: all existing tests pass. If any existing 429 test expected 3 retries for a non-capacity reason, it should still pass because we only fast-fail on specific reasons.

- [ ] **Step 6: Commit**

```bash
git add kiro/http_client.py tests/unit/test_http_client.py
git commit -m "feat(http): fast-fail INSUFFICIENT_MODEL_CAPACITY 429 to failover in <1s"
```

---

## Task 3: Retry mid-stream `RemoteProtocolError` via first-token retry loop

**Files:**
- Modify: `kiro/streaming_core.py:480-491` (replace the generic `except Exception` with a typed branch)
- Test: `tests/unit/test_streaming_core.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_streaming_core.py` (at the bottom):

```python
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock
from kiro.streaming_core import stream_with_first_token_retry

@pytest.mark.asyncio
async def test_remote_protocol_error_before_first_token_is_retried():
    calls = {"n": 0}

    async def make_request():
        calls["n"] += 1
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.aclose = AsyncMock()
        if calls["n"] == 1:
            async def _fail_iter():
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message body"
                )
                yield b""  # unreachable
            resp.aiter_bytes = _fail_iter
        else:
            async def _ok_iter():
                yield b'{"ok": true}'
            resp.aiter_bytes = _ok_iter
        return resp

    async def inner_parser(response, **kwargs):
        async for chunk in response.aiter_bytes():
            yield chunk

    chunks = []
    async for c in stream_with_first_token_retry(
        make_request=make_request,
        inner_parser=inner_parser,
        first_token_timeout=5.0,
        max_retries=3,
    ):
        chunks.append(c)

    assert calls["n"] == 2, "should retry once after RemoteProtocolError"
    assert chunks, "should yield after retry"
```

Run: `pytest tests/unit/test_streaming_core.py -k remote_protocol_error -v`
Expected: FAIL — current code re-raises on `RemoteProtocolError`.

- [ ] **Step 2: Add typed retry branch**

In `kiro/streaming_core.py`, before the final `except Exception`:

```python
        except httpx.RemoteProtocolError as e:
            last_error = e
            logger.warning(
                f"[RemoteProtocolError] Attempt {attempt + 1}/{max_retries} upstream disconnected mid-stream: {e}"
            )
            if response:
                try:
                    await response.aclose()
                except Exception:
                    pass
            if attempt >= max_retries - 1:
                raise
            continue

        except Exception as e:
            ...
```

The existing `except Exception` block stays for truly unknown errors.

- [ ] **Step 3: Run the new test and the whole file**

Run: `pytest tests/unit/test_streaming_core.py -v`
Expected: the new test passes, all existing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add kiro/streaming_core.py tests/unit/test_streaming_core.py
git commit -m "feat(stream): auto-retry mid-stream RemoteProtocolError instead of 500"
```

---

## Task 4: Add performance metrics to `RequestRecord`

**Files:**
- Modify: `kiro/control_panel.py:113-154` (RequestRecord dataclass) and add `record_metrics` / `record_trim` methods.
- Test: `tests/unit/test_control_panel.py`

- [ ] **Step 1: Write failing test**

```python
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
```

Run: `pytest tests/unit/test_control_panel.py -k record_metrics -v` → FAIL.

- [ ] **Step 2: Extend `RequestRecord`**

In `kiro/control_panel.py` dataclass:

```python
@dataclass
class RequestRecord:
    id: str
    api_format: str
    path: str
    stream: bool
    original_model: str
    routed_model: str
    active_model: str
    routing_reason: str
    status: str = "active"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    attempts: List[RequestAttempt] = field(default_factory=list)
    payloads: Dict[str, str] = field(default_factory=dict)
    chunks: List[str] = field(default_factory=list)
    response: Optional[str] = None
    error: Optional[str] = None
    ttft_s: Optional[float] = None
    tps: Optional[float] = None
    total_s: Optional[float] = None
    output_tokens: Optional[int] = None
    input_tokens: Optional[int] = None
    trim_before_messages: Optional[int] = None
    trim_after_messages: Optional[int] = None
    trim_before_bytes: Optional[int] = None
    trim_after_bytes: Optional[int] = None
```

Add methods on `ControlPanelState`:

```python
    def record_metrics(
        self,
        request_id: str,
        ttft: Optional[float] = None,
        tps: Optional[float] = None,
        total_s: Optional[float] = None,
        output_tokens: Optional[int] = None,
        input_tokens: Optional[int] = None,
    ) -> None:
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record:
                for r in self._completed_requests:
                    if r.id == request_id:
                        record = r
                        break
            if not record:
                return
            if ttft is not None:
                record.ttft_s = round(ttft, 3)
            if tps is not None:
                record.tps = round(tps, 2)
            if total_s is not None:
                record.total_s = round(total_s, 3)
            if output_tokens is not None:
                record.output_tokens = output_tokens
            if input_tokens is not None:
                record.input_tokens = input_tokens
            record.updated_at = time.time()

    def record_trim(
        self,
        request_id: str,
        before: int,
        after: int,
        before_bytes: int,
        after_bytes: int,
    ) -> None:
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record:
                return
            record.trim_before_messages = before
            record.trim_after_messages = after
            record.trim_before_bytes = before_bytes
            record.trim_after_bytes = after_bytes
            record.updated_at = time.time()
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/test_control_panel.py -v`
Expected: PASS (new + existing).

- [ ] **Step 4: Wire call sites**

Edit `kiro/streaming_anthropic.py` around line 712-717 (right after the `logger.info(...TTFT=...)`):

```python
        if _first_token_time:
            ttft = _first_token_time - _start_time
            total_dur = time.time() - _start_time
            gen_time = time.time() - _first_token_time
            tps = output_tokens / gen_time if gen_time > 0 else 0
            logger.info(f"[{model}] Stream finished (Anthropic): TTFT={ttft:.2f}s, TPS={tps:.1f} tok/s, Total={total_dur:.2f}s, Tokens={output_tokens}")
            try:
                from kiro.control_panel import control_panel
                rid = _monitor_request_id  # see wiring step below
                if rid:
                    control_panel.record_metrics(
                        rid, ttft=ttft, tps=tps, total_s=total_dur,
                        output_tokens=output_tokens, input_tokens=input_tokens,
                    )
            except Exception:
                pass
```

Propagate `monitor_request_id` into the streaming function signature. In `kiro/streaming_anthropic.py` `stream_kiro_to_anthropic`, add a keyword-only argument `monitor_request_id: Optional[str] = None` and pass it into local scope as `_monitor_request_id`. Update all callers in `kiro/routes_anthropic.py` (search for `stream_kiro_to_anthropic(` and `stream_with_first_token_retry_anthropic(`) to pass `monitor_request_id=monitor_request_id`.

Do the same for `kiro/streaming_openai.py` and `kiro/routes_openai.py`.

- [ ] **Step 5: Hook `record_trim` in converters**

In `kiro/converters_core.py`, around the `Trimmed conversation history` log (line 1604), add:

```python
            try:
                from kiro.control_panel import control_panel
                if monitor_request_id:
                    control_panel.record_trim(
                        monitor_request_id,
                        before=stats.original_entries,
                        after=stats.final_entries,
                        before_bytes=stats.original_bytes,
                        after_bytes=stats.final_bytes,
                    )
            except Exception:
                pass
```

Add `monitor_request_id: Optional[str] = None` to `build_kiro_payload` signature. Threads through from `anthropic_to_kiro` / `openai_to_kiro` and their callers in `routes_anthropic.py` / `routes_openai.py`.

- [ ] **Step 6: Run full unit suite for affected modules**

Run: `pytest tests/unit/test_streaming_anthropic.py tests/unit/test_streaming_openai.py tests/unit/test_converters_core.py tests/unit/test_control_panel.py -x`
Expected: PASS. If signature-change tests fail, update their call sites (keyword-only default, so most should be unaffected).

- [ ] **Step 7: Commit**

```bash
git add kiro/ tests/unit/
git commit -m "feat(monitor): record TTFT/TPS/tokens/trim on RequestRecord for dashboard"
```

---

## Task 5: Log ring buffer and live log endpoint

**Files:**
- Create: `kiro/log_buffer.py`
- Modify: `main.py:94-101` (install sink) and `kiro/routes_dashboard.py` (expose endpoint).
- Test: `tests/unit/test_log_buffer.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_log_buffer.py`:

```python
from kiro.log_buffer import LogRingBuffer

def test_ring_buffer_keeps_last_n():
    buf = LogRingBuffer(capacity=3)
    for i in range(5):
        buf.append({"seq": i, "msg": f"line {i}", "level": "INFO", "ts": float(i)})
    entries = buf.snapshot()
    assert len(entries) == 3
    assert [e["seq"] for e in entries] == [2, 3, 4]

def test_ring_buffer_since_seq():
    buf = LogRingBuffer(capacity=10)
    for i in range(5):
        buf.append({"seq": i, "msg": f"line {i}", "level": "INFO", "ts": 0.0})
    assert [e["seq"] for e in buf.since(2)] == [3, 4]
    assert buf.since(99) == []

def test_ring_buffer_subscribers_get_events():
    buf = LogRingBuffer(capacity=10)
    seen = []
    buf.subscribe(seen.append)
    buf.append({"seq": 0, "msg": "x", "level": "INFO", "ts": 0.0})
    assert len(seen) == 1
```

Run: `pytest tests/unit/test_log_buffer.py -v` → FAIL (module missing).

- [ ] **Step 2: Implement LogRingBuffer**

Create `kiro/log_buffer.py`:

```python
# -*- coding: utf-8 -*-

"""In-memory ring buffer for log lines, plus loguru sink + pub/sub fanout."""

from __future__ import annotations

from collections import deque
from itertools import count
from threading import RLock
from typing import Any, Callable, Deque, Dict, List, Optional


class LogRingBuffer:
    """Thread-safe ring buffer of recent log events with subscriber fanout."""

    def __init__(self, capacity: int = 2000):
        self._lock = RLock()
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._seq = count()
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []

    def append(self, entry: Dict[str, Any]) -> None:
        if "seq" not in entry:
            entry = {**entry, "seq": next(self._seq)}
        subs: List[Callable[[Dict[str, Any]], None]]
        with self._lock:
            self._entries.append(entry)
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(entry)
            except Exception:
                pass

    def snapshot(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            data = list(self._entries)
        return data if limit is None else data[-limit:]

    def since(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._entries if e.get("seq", -1) > seq]

    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass


log_buffer = LogRingBuffer(capacity=2000)


def loguru_sink(message) -> None:
    """Loguru sink that pushes each formatted record into the ring buffer."""
    record = message.record
    log_buffer.append({
        "ts": record["time"].timestamp(),
        "level": record["level"].name,
        "name": record["name"],
        "function": record["function"],
        "line": record["line"],
        "msg": record["message"],
    })
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/test_log_buffer.py -v` → PASS.

- [ ] **Step 4: Install sink in `main.py`**

After the existing `logger.add(sys.stderr, ...)` call:

```python
from kiro.log_buffer import loguru_sink as _log_sink
logger.add(_log_sink, level=LOG_LEVEL, format="{message}")
```

- [ ] **Step 5: Expose endpoint**

In `kiro/routes_dashboard.py` add:

```python
@router.get("/dashboard/api/logs", dependencies=[Security(verify_dashboard_api_key)])
async def get_dashboard_logs(since: int = -1, limit: int = 500) -> Dict[str, Any]:
    from kiro.log_buffer import log_buffer
    if since >= 0:
        entries = log_buffer.since(since)
    else:
        entries = log_buffer.snapshot(limit=limit)
    return {"entries": entries}
```

- [ ] **Step 6: Commit**

```bash
git add kiro/log_buffer.py main.py kiro/routes_dashboard.py tests/unit/test_log_buffer.py
git commit -m "feat(monitor): add in-memory log ring buffer and /dashboard/api/logs"
```

---

## Task 6: Metrics buckets (RPS / P50 / P95 / error rate)

**Files:**
- Create: `kiro/metrics.py`
- Modify: `kiro/control_panel.py` `finish_request` to tick metrics.
- Test: `tests/unit/test_metrics.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_metrics.py`:

```python
import time
from kiro.metrics import MetricsRegistry

def test_metrics_bucket_rps_and_latency():
    reg = MetricsRegistry(window_s=60, bucket_s=1)
    now = time.time()
    for _ in range(10):
        reg.record(now, duration_s=0.2, is_error=False)
    for _ in range(2):
        reg.record(now, duration_s=1.5, is_error=True)
    series = reg.snapshot(now=now)
    assert series["count"] == 12
    assert series["errors"] == 2
    assert 0.0 < series["p50"] <= 1.5
    assert series["p95"] >= series["p50"]

def test_metrics_drops_old_buckets():
    reg = MetricsRegistry(window_s=5, bucket_s=1)
    t0 = 1_000_000.0
    reg.record(t0, 0.1, False)
    series = reg.snapshot(now=t0 + 10)
    assert series["count"] == 0
```

Run: `pytest tests/unit/test_metrics.py -v` → FAIL.

- [ ] **Step 2: Implement metrics**

Create `kiro/metrics.py`:

```python
# -*- coding: utf-8 -*-

"""Rolling-window request metrics for dashboard sparklines."""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Deque, Dict, List, Tuple


class MetricsRegistry:
    """Ring of per-second buckets (count, errors, durations) over a window."""

    def __init__(self, window_s: int = 300, bucket_s: int = 5):
        self._window_s = window_s
        self._bucket_s = bucket_s
        self._lock = RLock()
        self._buckets: Deque[Tuple[int, List[float], int, int]] = deque()

    def _bucket_key(self, ts: float) -> int:
        return int(ts // self._bucket_s) * self._bucket_s

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()

    def record(self, ts: float, duration_s: float, is_error: bool) -> None:
        key = self._bucket_key(ts)
        with self._lock:
            self._prune(ts)
            if self._buckets and self._buckets[-1][0] == key:
                bucket = self._buckets[-1]
                bucket[1].append(duration_s)
                new_count = bucket[2] + 1
                new_err = bucket[3] + (1 if is_error else 0)
                self._buckets[-1] = (bucket[0], bucket[1], new_count, new_err)
            else:
                self._buckets.append((key, [duration_s], 1, 1 if is_error else 0))

    def snapshot(self, now: float) -> Dict[str, object]:
        with self._lock:
            self._prune(now)
            durations: List[float] = []
            count = 0
            errors = 0
            series: List[Dict[str, float]] = []
            for key, ds, c, e in self._buckets:
                durations.extend(ds)
                count += c
                errors += e
                series.append({"t": key, "count": c, "errors": e})
        if durations:
            sd = sorted(durations)
            p50 = sd[len(sd) // 2]
            p95 = sd[min(len(sd) - 1, int(len(sd) * 0.95))]
        else:
            p50 = 0.0
            p95 = 0.0
        return {
            "window_s": self._window_s,
            "bucket_s": self._bucket_s,
            "count": count,
            "errors": errors,
            "p50": p50,
            "p95": p95,
            "series": series,
        }


metrics_registry = MetricsRegistry(window_s=300, bucket_s=5)
```

- [ ] **Step 3: Tick in `finish_request`**

In `kiro/control_panel.py` `finish_request`, before `appendleft`:

```python
            try:
                from kiro.metrics import metrics_registry
                duration = (record.ended_at or now) - record.started_at
                is_error = status not in ("completed", "client_disconnected")
                metrics_registry.record(now, duration, is_error)
            except Exception:
                pass
```

- [ ] **Step 4: Expose endpoint**

In `kiro/routes_dashboard.py`:

```python
@router.get("/dashboard/api/metrics", dependencies=[Security(verify_dashboard_api_key)])
async def get_dashboard_metrics() -> Dict[str, Any]:
    from kiro.metrics import metrics_registry
    return metrics_registry.snapshot(now=time.time())
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/test_metrics.py tests/unit/test_control_panel.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add kiro/metrics.py kiro/control_panel.py kiro/routes_dashboard.py tests/unit/test_metrics.py
git commit -m "feat(monitor): add rolling RPS/P50/P95/error metrics"
```

---

## Task 7: Enrich account snapshot

**Files:**
- Modify: `kiro/account_manager.py:830-861` `get_accounts_snapshot`
- Test: `tests/unit/test_account_manager.py` (add one case)

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_account_manager.py`:

```python
def test_accounts_snapshot_has_cooldown_fields(tmp_path):
    import time
    from kiro.account_manager import AccountManager, Account, AccountStats

    mgr = AccountManager(credentials_file=str(tmp_path / "c.json"),
                         state_file=str(tmp_path / "s.json"))
    acc = Account(id="acc1", credential={}, stats=AccountStats())
    acc.failures = 2
    acc.last_failure_time = time.time() - 30
    mgr._accounts["acc1"] = acc
    mgr._current_account_index = 0

    snap = mgr.get_accounts_snapshot()
    assert len(snap) == 1
    entry = snap[0]
    assert entry["failures"] == 2
    assert entry["backoff_tier"] == 2
    assert entry["cooldown_total_s"] == 120  # 60 * 2^(2-1)
    assert 80 <= entry["cooldown_remaining_s"] <= 100
    assert "last_error_reason" in entry
```

Run: `pytest tests/unit/test_account_manager.py -k cooldown_fields -v` → FAIL.

- [ ] **Step 2: Track last error reason on Account**

In `kiro/account_manager.py` add to `Account` dataclass:

```python
@dataclass
class Account:
    # existing fields...
    last_error_reason: Optional[str] = None
    last_error_status: Optional[int] = None
```

In `report_failure`, record reason/status before `self._dirty = True`:

```python
            account.last_error_reason = reason
            account.last_error_status = status_code
```

- [ ] **Step 3: Extend snapshot**

Replace the body of `get_accounts_snapshot`:

```python
    def get_accounts_snapshot(self) -> List[Dict[str, Any]]:
        from dataclasses import asdict
        now = time.time()
        snapshot: List[Dict[str, Any]] = []
        all_account_ids = list(self._accounts.keys())

        for idx, account_id in enumerate(all_account_ids):
            account = self._accounts[account_id]

            display_id = account_id
            if os.sep in account_id:
                display_id = os.path.basename(account_id)
            elif account_id.startswith("refresh_token_"):
                display_id = "Token Account (" + account_id[-8:] + ")"

            if account.failures > 0:
                backoff_mult = min(
                    2 ** (account.failures - 1),
                    ACCOUNT_MAX_BACKOFF_MULTIPLIER,
                )
                cooldown_total = ACCOUNT_RECOVERY_TIMEOUT * backoff_mult
                elapsed = now - account.last_failure_time
                cooldown_remaining = max(0, int(cooldown_total - elapsed))
            else:
                backoff_mult = 0
                cooldown_total = 0
                cooldown_remaining = 0

            current_models: List[str] = []
            if account.model_resolver:
                try:
                    current_models = list(account.model_resolver.get_available_models())
                except Exception:
                    current_models = []

            snapshot.append({
                "id": account_id,
                "display_id": display_id,
                "failures": account.failures,
                "last_failure_time": account.last_failure_time,
                "models_cached_at": account.models_cached_at,
                "stats": asdict(account.stats),
                "is_initialized": account.auth_manager is not None,
                "is_current": idx == self._current_account_index,
                "backoff_tier": account.failures,
                "backoff_multiplier": backoff_mult,
                "cooldown_total_s": cooldown_total,
                "cooldown_remaining_s": cooldown_remaining,
                "last_error_reason": account.last_error_reason,
                "last_error_status": account.last_error_status,
                "available_models_count": len(current_models),
            })
        return snapshot
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_account_manager.py -x`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add kiro/account_manager.py tests/unit/test_account_manager.py
git commit -m "feat(accounts): expose cooldown, backoff tier, last error in snapshot"
```

---

## Task 8: SSE events endpoint

**Files:**
- Modify: `kiro/control_panel.py` — add `subscribe`/`unsubscribe` + emit events on state changes.
- Modify: `kiro/routes_dashboard.py` — add `/dashboard/api/events`.
- Test: `tests/unit/test_sse_events.py`

- [ ] **Step 1: Write failing test (HTTP-level)**

Create `tests/unit/test_sse_events.py`:

```python
import asyncio
import pytest
from fastapi.testclient import TestClient
from kiro.routes_dashboard import router
from kiro.config import PROXY_API_KEY
from fastapi import FastAPI


def test_events_endpoint_requires_auth():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    with client.stream("GET", "/dashboard/api/events") as resp:
        assert resp.status_code == 401


def test_events_endpoint_streams_snapshot_first():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {PROXY_API_KEY}"}
    with client.stream("GET", "/dashboard/api/events", headers=headers) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        chunks = []
        for raw in resp.iter_text():
            chunks.append(raw)
            if "event: snapshot" in "".join(chunks):
                break
        joined = "".join(chunks)
        assert "event: snapshot" in joined
```

Run: `pytest tests/unit/test_sse_events.py -v` → FAIL (endpoint missing).

- [ ] **Step 2: Add subscribe hooks to control panel**

In `kiro/control_panel.py` add to `__init__`:

```python
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
```

And methods:

```python
    def subscribe(self, fn):
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn):
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn({"event": event, "data": data})
            except Exception:
                pass
```

Call `self._emit("request_started", ...)` in `start_request`, `self._emit("attempt", ...)` in `start_attempt`/`finish_attempt`, `self._emit("request_finished", ...)` in `finish_request`. Pass the minimal `asdict(record)` payload.

- [ ] **Step 3: Add `/dashboard/api/events`**

In `kiro/routes_dashboard.py`:

```python
from fastapi.responses import StreamingResponse
import asyncio
import json as _json

@router.get("/dashboard/api/events", dependencies=[Security(verify_dashboard_api_key)])
async def dashboard_events(request: Request):
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
            yield f"event: snapshot\ndata: {_json.dumps(snap)}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {evt['event']}\ndata: {_json.dumps(evt['data'])}\n\n"
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_sse_events.py tests/unit/test_control_panel.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add kiro/control_panel.py kiro/routes_dashboard.py tests/unit/test_sse_events.py
git commit -m "feat(dashboard): SSE /dashboard/api/events replaces 1.2s polling"
```

---

## Task 9: Rebuild dashboard UI (sidebar + density + live log + sparklines)

Keep the existing light paper/accent color palette per the user's preference — no dark ops mode. Redesign for density and utility.

**Files:**
- Modify: `kiro/routes_dashboard.py` (the `DASHBOARD_HTML` block, starting line 266).

- [ ] **Step 1: Design notes (no code change)**

Layout:
```
┌──────────┬─────────────────────────────────────────────────────────┐
│ sidebar  │  content                                                │
│          │                                                         │
│ - Status │  [ Status stripe: RPS • P50 • P95 • err%  |  sparkline ]│
│ - Routing│                                                         │
│ - Accts  │  [ Accounts row: cards with cooldown countdown ]        │
│ - Active │                                                         │
│ - History│  [ Active requests ] [ Completed (virtual scroll) ]     │
│ - Logs   │                                                         │
│          │  [ Live log console: filter bar + autoscroll ]          │
└──────────┴─────────────────────────────────────────────────────────┘
```

Same palette: `--paper: #f5f1e8; --ink: #18211f; --accent: #c45f2c;`. Fonts and borders are kept; switch spacing to 6px grid for density.

- [ ] **Step 2: Replace `DASHBOARD_HTML`**

Replace the whole multi-line string assigned to `DASHBOARD_HTML` (from the `"""<!doctype html>` opening to the closing `"""`). Full content:

```html
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kiro Gateway Console</title>
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
    <div class="ver">vAPP_VERSION_PLACEHOLDER · <span id="connStatus"><span class="status-dot off"></span>disconnected</span></div>
    <a data-jump="panel-status" class="active">Overview</a>
    <a data-jump="panel-routing">Routing</a>
    <a data-jump="panel-accounts">Accounts</a>
    <a data-jump="panel-active">Active</a>
    <a data-jump="panel-history">History</a>
    <a data-jump="panel-logs">Live Log</a>
    <div class="key">
      <label>Proxy API Key</label>
      <input id="apiKey" type="password" placeholder="Bearer key">
      <div style="margin-top:6px"><button class="btn primary" onclick="saveKey()">Connect</button><button class="btn ghost" onclick="forgetKey()">Clear</button></div>
    </div>
  </nav>
  <main>
    <section id="panel-status" class="panel">
      <header><h2>Overview (last 5 min)</h2><div id="stripeMeta" class="muted" style="font-size:11px;color:var(--muted)"></div></header>
      <div class="stripe">
        <div class="stat"><div class="lbl">RPS</div><div class="val" id="mRps">—</div></div>
        <div class="stat"><div class="lbl">P50 latency</div><div class="val" id="mP50">—</div></div>
        <div class="stat"><div class="lbl">P95 latency</div><div class="val" id="mP95">—</div></div>
        <div class="stat"><div class="lbl">Error rate</div><div class="val" id="mErr">—</div></div>
        <div class="stat"><div class="lbl">Active</div><div class="val" id="mActive">—</div></div>
        <div class="spark"><svg id="sparkRps" viewBox="0 0 200 36" preserveAspectRatio="none"></svg><svg id="sparkErr" viewBox="0 0 200 36" preserveAspectRatio="none"></svg></div>
      </div>
    </section>

    <section id="panel-routing" class="panel">
      <header><h2>Routing</h2><div class="toolbar">
        <button class="btn" onclick="quickSwap('claude-opus-4.6')">force 4.6</button>
        <button class="btn" onclick="quickSwap('claude-opus-4.7')">force 4.7</button>
        <button class="btn warn" onclick="resetRouting()">reset</button>
      </div></header>
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px">
        <label class="chip"><input id="enabled" type="checkbox"> enable routing</label>
        <label class="chip"><input id="safeFallback" type="checkbox"> retry original on fail</label>
        <label class="chip"><input id="fallbackEnabled" type="checkbox"> model fallback</label>
        <label class="chip"><input id="captureContent" type="checkbox"> capture payloads</label>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px">
        <div><label style="font-size:10.5px;color:var(--muted)">Mode</label>
          <select id="mode" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"><option value="passthrough">Passthrough</option><option value="manual">Manual</option><option value="redirect">Redirect</option></select></div>
        <div><label style="font-size:10.5px;color:var(--muted)">Manual model</label>
          <input id="manualModel" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px" placeholder="claude-opus-4.6"></div>
        <div><label style="font-size:10.5px;color:var(--muted)">Fallback models (comma)</label>
          <input id="fallbackModels" style="width:100%;padding:5px;border:1px solid var(--line);border-radius:6px"></div>
      </div>
      <label style="font-size:10.5px;color:var(--muted);display:block;margin-top:8px">Redirect rules JSON</label>
      <textarea id="redirects" style="width:100%;min-height:64px;padding:6px;border:1px solid var(--line);border-radius:6px;font:12px 'SF Mono',Menlo,monospace"></textarea>
      <div style="margin-top:8px"><button class="btn primary" onclick="applyRouting()">Apply</button><span id="saveResult" style="margin-left:10px;color:var(--muted)"></span></div>
    </section>

    <section id="panel-accounts" class="panel">
      <header><h2>Accounts <span id="accountCount" class="tag">0</span></h2></header>
      <div id="accounts" class="accounts"><p style="color:var(--muted)">Waiting for data…</p></div>
    </section>

    <section id="panel-active" class="panel">
      <header><h2>Active requests</h2><span id="activeCount" class="tag">0</span></header>
      <div id="activeWrap"><p style="color:var(--muted)">No active requests.</p></div>
    </section>

    <section id="panel-history" class="panel">
      <header><h2>History</h2>
        <div class="toolbar">
          <input id="histFilter" class="search" placeholder="filter: model / status / account / error">
          <button class="btn ghost" onclick="clearMonitor()">clear</button>
        </div>
      </header>
      <div id="historyWrap" style="max-height:520px;overflow:auto"></div>
    </section>

    <section id="panel-logs" class="panel">
      <header><h2>Live Log</h2>
        <div class="toolbar">
          <span class="filter-chip on" data-level="ALL">all</span>
          <span class="filter-chip" data-level="WARNING">warn+</span>
          <span class="filter-chip" data-level="ERROR">error only</span>
        </div>
      </header>
      <div class="log-console">
        <header>
          <input class="filter" id="logSearch" placeholder="grep (substring, case-insensitive)">
          <label><input id="logAutoScroll" type="checkbox" checked> autoscroll</label>
          <button class="btn" onclick="clearLogView()">clear view</button>
        </header>
        <div class="body" id="logBody"></div>
      </div>
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
async function api(p,o={}){if(!hasKey())throw new Error("enter api key first");
  const r=await fetch(p,{...o,headers:{...authHeaders(),...(o.headers||{})}});
  if(r.status===401)throw new Error("unauthorized");
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
const state={routing:null,accounts:[],active:{},completed:[],metrics:null,logs:[],logSeq:-1};
const LOG_MAX_VIEW=2000;let logLevelFilter="ALL";let histFilterText="";

function fmtTime(ts){if(!ts)return"";return new Date(ts*1000).toLocaleTimeString();}
function fmtMs(s){if(s==null)return"—";return s<1?`${(s*1000).toFixed(0)}ms`:`${s.toFixed(2)}s`;}
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);}

function saveKey(){if(!hasKey()){setConn(false,"no key");return;}
  localStorage.setItem("kiro-dashboard-key",$("#apiKey").value.trim());connect();}
function forgetKey(){localStorage.removeItem("kiro-dashboard-key");$("#apiKey").value="";if(es)es.close();setConn(false,"disconnected");}

// -------- SSE --------
let es=null;
function connect(){
  if(es)es.close();
  if(!hasKey()){setConn(false,"no key");return;}
  const url=`/dashboard/api/events?_auth=${encodeURIComponent($("#apiKey").value.trim())}`;
  // fall back to fetch+reader because EventSource can't set headers
  streamEvents();
  pullMetrics();setInterval(pullMetrics,5000);
  pullLogs();
}
async function streamEvents(){
  setConn(null,"connecting");
  try{
    const r=await fetch("/dashboard/api/events",{headers:authHeaders()});
    if(r.status===401){setConn(false,"unauthorized");return;}
    setConn(true,"live");
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
    setConn(false,"stream closed");setTimeout(streamEvents,2000);
  }catch(e){setConn(false,"err: "+e.message);setTimeout(streamEvents,3000);}
}

function handleEvent(ev,d){
  if(ev==="snapshot"){
    state.routing=d.routing;state.accounts=d.accounts||[];
    (d.active_requests||[]).forEach(r=>state.active[r.id]=r);
    state.completed=d.completed_requests||[];
    writeRoutingForm(state.routing);renderAll();
  }else if(ev==="request_started"){state.active[d.id]=d;renderActive();}
   else if(ev==="attempt"){if(state.active[d.id]){state.active[d.id]=d;renderActive();}}
   else if(ev==="request_finished"){delete state.active[d.id];state.completed.unshift(d);if(state.completed.length>200)state.completed.pop();renderActive();renderHistory();}
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
  $("#stripeMeta").textContent=`${m.count} reqs · ${m.errors} err · window=${m.window_s}s`;
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
  const el=$("#accounts");if(!state.accounts.length){el.innerHTML=`<p style="color:var(--muted)">No accounts.</p>`;return;}
  $("#accountCount").textContent=state.accounts.length;
  el.innerHTML=state.accounts.map(a=>{
    const cur=a.is_current?'current':'';
    const cool=a.cooldown_remaining_s>0
      ? `<div class="cooldown">cooldown ${a.cooldown_remaining_s}s / ${a.cooldown_total_s}s (tier ${a.backoff_tier})</div>`:"";
    const lastErr=a.last_error_reason?`<div style="font-size:10.5px;color:var(--muted)">last: ${esc(a.last_error_reason)} (${a.last_error_status||"-"})</div>`:"";
    return `<div class="acct ${cur}">
      <div class="name"><span>${esc(a.display_id)}</span><span class="tag ${a.is_current?'on':''} ${a.failures>0?'err':''}">${a.is_current?'ACTIVE':(a.failures>0?'COOLDOWN':'STANDBY')}</span></div>
      <dl><dt>total</dt><dt>ok</dt><dt>fail</dt>
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
  if(!rows.length){el.innerHTML=`<p style="color:var(--muted)">No active requests.</p>`;return;}
  el.innerHTML=`<table class="reqs"><thead><tr><th>started</th><th>status</th><th>api</th><th>model</th><th>ttft</th><th>tps</th><th>tok</th><th>try</th><th>trim</th><th>err</th></tr></thead>
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
  if(!rows.length){el.innerHTML=`<p style="color:var(--muted)">No completed requests.</p>`;return;}
  el.innerHTML=`<table class="reqs"><thead><tr><th>started</th><th>status</th><th>api</th><th>model</th><th>ttft</th><th>tps</th><th>tok</th><th>try</th><th>trim</th><th>err</th></tr></thead>
    <tbody>${rows.slice(0,300).map(r=>reqRow(r,false)).join("")}</tbody></table>`;
  wireRowExpand("#historyWrap","completed");
}

$("#histFilter").addEventListener("input",e=>{histFilterText=e.target.value;renderHistory();});

function wireRowExpand(wrapSel,kind){
  $(wrapSel).querySelectorAll("tr.row").forEach(tr=>{
    tr.addEventListener("click",()=>{
      const next=tr.nextElementSibling;
      if(next&&next.classList.contains("exp")){next.remove();return;}
      const id=tr.dataset.id;const r=(kind==="active"?state.active[id]:state.completed.find(x=>x.id===id));
      if(!r)return;
      const exp=document.createElement("tr");exp.className="exp";
      exp.innerHTML=`<td colspan="10">${renderDetail(r)}</td>`;
      tr.after(exp);
    });
  });
}

function renderDetail(r){
  const attempts=(r.attempts||[]).map(a=>`<div>${esc(a.model)} · ${a.account_id||"-"} · ${a.http_status||"-"} · ${esc(a.status)}${a.error?` · ${esc(a.error)}`:""}</div>`).join("");
  const payloads=Object.entries(r.payloads||{}).map(([n,b])=>{
    const big=b&&b.length>40000;
    return `<details class="payload"><summary>${esc(n)}${big?" (large)":""}</summary>${big?`<p style="color:var(--muted)">${b.length} chars — open to load</p><button class="btn" onclick="this.nextElementSibling.classList.remove('hidden');this.remove()">Show</button><pre class="code hidden">${esc(b)}</pre>`:`<pre class="code">${esc(b)}</pre>`}</details>`;
  }).join("");
  const chunks=(r.chunks||[]).length?`<details class="payload"><summary>stream chunks (${r.chunks.length})</summary><pre class="code">${esc(r.chunks.join("\n\n"))}</pre></details>`:"";
  const resp=r.response?`<details class="payload"><summary>response</summary><pre class="code">${esc(r.response)}</pre></details>`:"";
  return `<div style="padding:6px 4px"><div style="color:var(--muted);margin-bottom:4px">id=${esc(r.id)} · reason=${esc(r.routing_reason||"")}</div>
    <div style="margin-bottom:4px"><strong>attempts</strong>${attempts||" —"}</div>${payloads}${chunks}${resp}</div>`;
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
function readRoutingForm(){let red={};try{red=JSON.parse($("#redirects").value||"{}")}catch(e){throw new Error("redirects must be JSON");}
  return{enabled:$("#enabled").checked,mode:$("#mode").value,manual_model:$("#manualModel").value,redirects:red,
    fallback_enabled:$("#fallbackEnabled").checked,
    fallback_models:$("#fallbackModels").value.split(",").map(v=>v.trim()).filter(Boolean),
    safe_fallback_to_original:$("#safeFallback").checked,capture_content:$("#captureContent").checked};}
async function applyRouting(){try{const d=await api("/dashboard/api/routing",{method:"PUT",body:JSON.stringify(readRoutingForm())});
  writeRoutingForm(d.routing);$("#saveResult").textContent="applied "+new Date().toLocaleTimeString();}catch(e){$("#saveResult").textContent=e.message;}}
async function quickSwap(m){$("#enabled").checked=true;$("#mode").value="manual";$("#manualModel").value=m;await applyRouting();}
async function resetRouting(){await api("/dashboard/api/routing/reset",{method:"POST",body:"{}"});$("#saveResult").textContent="reset";}
async function clearMonitor(){await api("/dashboard/api/monitor/clear",{method:"POST",body:"{}"});state.completed=[];renderHistory();}

if(hasKey())connect();else setConn(false,"enter api key");
</script>
</body></html>
```

- [ ] **Step 3: Manual smoke test**

Open http://127.0.0.1:8000/dashboard — sidebar should render, API key auto-fills locally, SSE connects, stripe updates within 5 s, accounts show cooldown, live log streams, filter works.

- [ ] **Step 4: Commit**

```bash
git add kiro/routes_dashboard.py
git commit -m "feat(dashboard): sidebar UI with SSE, metrics stripe, live log, history filter"
```

---

## Task 10: Deploy

The user said short downtime is acceptable. Current server PID 78173, run as `.venv/bin/python main.py --host 0.0.0.0 --port 8000` from PPID 24802 (a terminal/tmux). Deploy plan keeps downtime under ~5 s.

- [ ] **Step 1: Final local check**

Run: `pytest tests/unit -x -q`
Expected: all green.

Run: `python -c "from kiro.routes_dashboard import DASHBOARD_HTML; assert len(DASHBOARD_HTML) > 1000"`
Expected: prints nothing, exit code 0.

- [ ] **Step 2: Quick syntax check**

Run: `python -m compileall -q main.py kiro/`
Expected: no errors.

- [ ] **Step 3: Stop the old server**

Ask the user to run it in their own terminal (the one holding PID 78173) — do **not** kill it from this session because PPID 24802 is their shell; killing from a sub-shell would orphan tty state.

Message to the user at deploy time:
> Ready to deploy. In the terminal running the current server, press **Ctrl+C**, then run:
> `.venv/bin/python main.py --host 0.0.0.0 --port 8000`
> Total downtime ≈ 3-5 s. I'll wait for your confirmation.

- [ ] **Step 4: Verify**

Run from this session:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/dashboard
curl -s -H "Authorization: Bearer $PROXY_API_KEY" http://127.0.0.1:8000/dashboard/api/metrics
```

Expected: `200` for the dashboard, JSON body with `count`, `p50`, `p95`, `series` keys.

- [ ] **Step 5: Tail new logs via the ring buffer**

Open the dashboard, scroll to Live Log panel, confirm entries flow and the `/dashboard/api/state` / `/dashboard/api/events` lines are absent from the uvicorn access log stream.

- [ ] **Step 6: Commit deploy notes (optional)**

No code change; skip commit if nothing was tweaked during deploy.
