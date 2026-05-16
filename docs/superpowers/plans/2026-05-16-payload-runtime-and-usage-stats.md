# Payload Runtime + Usage Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add (A) dashboard-controlled payload-size runtime toggle plus per-request input-tokens/payload-bytes display, and (B) a cumulative usage statistics panel — without disrupting in-flight requests.

**Architecture:** ControlPanelState owns the runtime payload settings (extends the existing `routing_state.json` persistence). The payload builder `build_kiro_payload` reads settings via an injected `dict` parameter passed from each route handler. `RequestRecord` gains a `request_bytes` field set unconditionally at send time. `MetricsRegistry` gains cumulative counters and a `by_model` breakdown, populated from a single hook fired on request completion. Two new dashboard endpoints + UI widgets.

**Tech Stack:** Python 3.10+, FastAPI, asyncio, pytest, pytest-asyncio, dataclasses, vanilla JS in the dashboard HTML string.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `kiro/control_panel.py` | runtime control-plane state + persistence | + `PayloadConfig`, getter/setter, load/save extension; + `request_bytes` field on `RequestRecord`; + hook into completion site |
| `kiro/converters_core.py` | `build_kiro_payload` builder | + accept `payload_settings: Optional[dict]`; replace value-imports with parameter reads; set `request_bytes` |
| `kiro/converters_anthropic.py` | Anthropic→Kiro adapter | + forward `payload_settings` to `build_kiro_payload` |
| `kiro/converters_openai.py` | OpenAI→Kiro adapter | + forward `payload_settings` |
| `kiro/routes_openai.py` | OpenAI routes | + pass `request.app.state.control_panel.get_payload_settings()` to builder calls |
| `kiro/routes_anthropic.py` | Anthropic routes | + same |
| `kiro/routes_dashboard.py` | Dashboard API + HTML/JS | + 2 routes (payload-settings, totals); + 2 UI sections; + table columns |
| `kiro/metrics.py` | metrics ring + cumulative | + cumulative counters, by_model, `record_completion()` |
| `tests/unit/test_control_panel.py` | control panel tests | + payload-settings tests |
| `tests/unit/test_metrics.py` | metrics tests (new file) | new file for cumulative counter tests |

---

## Task 1: Payload settings state + persistence + dashboard route

**Files:**
- Modify: `kiro/control_panel.py` (add `PayloadConfig`, methods, extend persistence)
- Modify: `kiro/routes_dashboard.py` (add GET/PUT route)
- Modify: `tests/unit/test_control_panel.py` (new test class)

- [ ] **Step 1: Write the failing tests**

Append to the end of `tests/unit/test_control_panel.py`:

```python
class TestPayloadSettings:
    """Runtime-mutable payload settings via ControlPanelState."""

    def test_defaults_seeded_from_env(self, tmp_path, monkeypatch):
        """Fresh ControlPanelState seeds payload settings from config defaults."""
        monkeypatch.setattr("kiro.control_panel.DEFAULT_MAX_PAYLOAD_BYTES", 600000)
        monkeypatch.setattr("kiro.control_panel.DEFAULT_AUTO_TRIM_PAYLOAD", True)
        monkeypatch.setattr(
            "kiro.control_panel.ControlPanelState.ROUTING_STATE_FILE",
            str(tmp_path / "routing_state.json"),
        )
        cp = ControlPanelState(persist=False)
        s = cp.get_payload_settings()
        assert s == {"max_bytes": 600000, "auto_trim": True}

    def test_set_payload_settings_updates_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro.control_panel.ControlPanelState.ROUTING_STATE_FILE",
            str(tmp_path / "routing_state.json"),
        )
        cp = ControlPanelState(persist=False)
        cp.set_payload_settings(max_bytes=300000, auto_trim=False)
        assert cp.get_payload_settings() == {"max_bytes": 300000, "auto_trim": False}

    def test_set_payload_settings_rejects_out_of_range(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro.control_panel.ControlPanelState.ROUTING_STATE_FILE",
            str(tmp_path / "routing_state.json"),
        )
        cp = ControlPanelState(persist=False)
        import pytest
        with pytest.raises(ValueError):
            cp.set_payload_settings(max_bytes=10, auto_trim=True)
        with pytest.raises(ValueError):
            cp.set_payload_settings(max_bytes=9_999_999, auto_trim=True)

    def test_payload_settings_persist_and_reload(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "routing_state.json")
        monkeypatch.setattr("kiro.control_panel.ControlPanelState.ROUTING_STATE_FILE", state_file)
        cp1 = ControlPanelState(persist=True)
        cp1.set_payload_settings(max_bytes=450000, auto_trim=False)

        cp2 = ControlPanelState(persist=True)
        assert cp2.get_payload_settings() == {"max_bytes": 450000, "auto_trim": False}

    def test_load_state_without_payload_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """Old routing_state.json files (no payload key) use env defaults."""
        import json as _json
        state_file = tmp_path / "routing_state.json"
        state_file.write_text(_json.dumps({"routing": {}, "throttle": {}}))
        monkeypatch.setattr("kiro.control_panel.DEFAULT_MAX_PAYLOAD_BYTES", 600000)
        monkeypatch.setattr("kiro.control_panel.DEFAULT_AUTO_TRIM_PAYLOAD", True)
        monkeypatch.setattr("kiro.control_panel.ControlPanelState.ROUTING_STATE_FILE", str(state_file))
        cp = ControlPanelState(persist=True)
        assert cp.get_payload_settings() == {"max_bytes": 600000, "auto_trim": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jamexhuang/Documents/github/kiro-gateway && pytest tests/unit/test_control_panel.py::TestPayloadSettings -v`
Expected: AttributeError / missing names.

- [ ] **Step 3: Add `PayloadConfig` dataclass and module defaults**

In `kiro/control_panel.py`, near the top with the other dataclasses (search for `@dataclass\nclass RoutingConfig`), add:

```python
from kiro.config import KIRO_MAX_PAYLOAD_BYTES as _ENV_MAX_PAYLOAD_BYTES
from kiro.config import AUTO_TRIM_PAYLOAD as _ENV_AUTO_TRIM_PAYLOAD

# Module-level defaults (env-driven, used as bootstrap before state.json loads).
# Tests monkeypatch these.
DEFAULT_MAX_PAYLOAD_BYTES: int = _ENV_MAX_PAYLOAD_BYTES
DEFAULT_AUTO_TRIM_PAYLOAD: bool = _ENV_AUTO_TRIM_PAYLOAD

# Hard bounds for set_payload_settings validation.
_PAYLOAD_MAX_BYTES_MIN: int = 50_000
_PAYLOAD_MAX_BYTES_MAX: int = 2_000_000


@dataclass
class PayloadConfig:
    """Runtime payload-size controls."""
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    auto_trim: bool = DEFAULT_AUTO_TRIM_PAYLOAD
```

If `from kiro.config import KIRO_MAX_PAYLOAD_BYTES, AUTO_TRIM_PAYLOAD` is already imported elsewhere in `control_panel.py`, deduplicate.

- [ ] **Step 4: Add `_payload` attribute + getter/setter on `ControlPanelState`**

Inside `class ControlPanelState`'s `__init__` (after `self._throttle = ThrottleConfig()`), add:

```python
        self._payload = PayloadConfig(
            max_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            auto_trim=DEFAULT_AUTO_TRIM_PAYLOAD,
        )
```

Add two new public methods on `ControlPanelState` (place them near `set_routing_config`):

```python
    def get_payload_settings(self) -> Dict[str, Any]:
        """Return current runtime payload settings."""
        with self._lock:
            return {"max_bytes": self._payload.max_bytes, "auto_trim": self._payload.auto_trim}

    def set_payload_settings(self, max_bytes: int, auto_trim: bool) -> None:
        """Update runtime payload settings.

        Raises:
            ValueError: if max_bytes outside [50_000, 2_000_000].
        """
        if not isinstance(max_bytes, int) or max_bytes < _PAYLOAD_MAX_BYTES_MIN or max_bytes > _PAYLOAD_MAX_BYTES_MAX:
            raise ValueError(
                f"max_bytes must be int in [{_PAYLOAD_MAX_BYTES_MIN}, {_PAYLOAD_MAX_BYTES_MAX}], got {max_bytes!r}"
            )
        with self._lock:
            self._payload = PayloadConfig(max_bytes=max_bytes, auto_trim=bool(auto_trim))
        self._save_routing_state()
        logger.info(f"Payload settings updated: max_bytes={max_bytes}, auto_trim={auto_trim}")
```

- [ ] **Step 5: Extend `_load_routing_state` and `_save_routing_state`**

Inside `_load_routing_state` (after the throttle restore block), add:

```python
                payload_data = data.get("payload")
                if payload_data:
                    allowed = set(PayloadConfig.__dataclass_fields__.keys())
                    filtered = {k: v for k, v in payload_data.items() if k in allowed}
                    self._payload = PayloadConfig(**filtered)
                    logger.info(f"Loaded persisted payload: max_bytes={self._payload.max_bytes}, auto_trim={self._payload.auto_trim}")
```

Inside `_save_routing_state`, replace the `data = {...}` block with:

```python
            data = {
                "routing": asdict(self._routing),
                "throttle": asdict(self._throttle),
                "payload": asdict(self._payload),
            }
```

- [ ] **Step 6: Add dashboard GET/PUT routes**

In `kiro/routes_dashboard.py`, immediately after the existing `set_latency_tracing()` function (around line 360), add:

```python
@router.get("/dashboard/api/payload-settings", dependencies=[Security(verify_dashboard_api_key)])
async def get_payload_settings(request: Request) -> Dict[str, Any]:
    """Return current runtime payload settings."""
    cp = request.app.state.control_panel
    return cp.get_payload_settings()


@router.put("/dashboard/api/payload-settings", dependencies=[Security(verify_dashboard_api_key)])
async def set_payload_settings(request: Request) -> Dict[str, Any]:
    """Update runtime payload settings.

    Does NOT affect in-flight requests — only the next call to build_kiro_payload()
    observes the new value.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    max_bytes = body.get("max_bytes")
    auto_trim = body.get("auto_trim")
    if not isinstance(max_bytes, int):
        raise HTTPException(status_code=400, detail="max_bytes must be an integer")
    if not isinstance(auto_trim, bool):
        raise HTTPException(status_code=400, detail="auto_trim must be a boolean")
    cp = request.app.state.control_panel
    try:
        cp.set_payload_settings(max_bytes=max_bytes, auto_trim=auto_trim)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return cp.get_payload_settings()
```

- [ ] **Step 7: Run tests to verify pass**

```bash
cd /Users/jamexhuang/Documents/github/kiro-gateway
pytest tests/unit/test_control_panel.py -q
pytest tests/unit/test_account_manager.py -q  # regression check
python -c "import kiro.routes_dashboard; print('OK')"
```

Expected: TestPayloadSettings 5 passed, full file passes, account tests unchanged (50 passed), import OK.

- [ ] **Step 8: Commit**

```bash
rtk git add kiro/control_panel.py kiro/routes_dashboard.py tests/unit/test_control_panel.py
rtk git commit -m "feat(payload): runtime-mutable payload settings with dashboard API"
```

---

## Task 2: Plumb payload settings into the builder + add `request_bytes`

**Files:**
- Modify: `kiro/converters_core.py` (`build_kiro_payload` signature + body)
- Modify: `kiro/converters_anthropic.py` (forward parameter)
- Modify: `kiro/converters_openai.py` (forward parameter)
- Modify: `kiro/routes_openai.py` (pass settings)
- Modify: `kiro/routes_anthropic.py` (pass settings)
- Modify: `kiro/control_panel.py` (`request_bytes` field + setter)
- Modify: `tests/unit/test_control_panel.py` (test for `record_request_bytes`)

- [ ] **Step 1: Write the failing test for `record_request_bytes`**

Append to `TestPayloadSettings` class:

```python
    def test_record_request_bytes_sets_field(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro.control_panel.ControlPanelState.ROUTING_STATE_FILE", str(tmp_path / "rs.json"))
        cp = ControlPanelState(persist=False)
        req_id = cp.start_request(
            request_id="r1",
            api_format="anthropic",
            stream=False,
            original_model="claude-opus-4.5",
            routed_model="claude-opus-4.5",
            active_model="claude-opus-4.5",
            routing_reason="manual",
        )
        cp.record_request_bytes(req_id, 12345)
        snap = cp.snapshot()
        active = snap.get("active", [])
        assert len(active) == 1
        assert active[0]["request_bytes"] == 12345
```

Note: if `start_request` signature differs, adapt minimally; the goal is to exercise `record_request_bytes`.

- [ ] **Step 2: Run test to confirm failure**

Run: `cd /Users/jamexhuang/Documents/github/kiro-gateway && pytest tests/unit/test_control_panel.py::TestPayloadSettings::test_record_request_bytes_sets_field -v`
Expected: AttributeError no `record_request_bytes`.

- [ ] **Step 3: Add `request_bytes` field to `RequestRecord`**

In `kiro/control_panel.py`, find `@dataclass\nclass RequestRecord`. After `trim_after_bytes: Optional[int] = None` (currently line ~180), add:

```python
    request_bytes: Optional[int] = None  # raw payload size sent upstream (always set)
```

- [ ] **Step 4: Add `record_request_bytes` method**

Add near `record_trim`:

```python
    def record_request_bytes(self, request_id: str, request_bytes: int) -> None:
        """Record the raw payload byte-count sent upstream for a request."""
        with self._lock:
            record = self._active_requests.get(request_id) or next(
                (r for r in self._completed_requests if r.id == request_id), None
            )
            if record is None:
                return
            record.request_bytes = int(request_bytes)
            record.updated_at = time.time()
            self._emit("request_updated", asdict(record))
```

- [ ] **Step 5: Run test to verify pass**

Run: `cd /Users/jamexhuang/Documents/github/kiro-gateway && pytest tests/unit/test_control_panel.py::TestPayloadSettings::test_record_request_bytes_sets_field -v`
Expected: PASS.

- [ ] **Step 6: Update `build_kiro_payload` signature + body**

In `kiro/converters_core.py`:

(a) Remove the value-imports for `KIRO_MAX_PAYLOAD_BYTES` and `AUTO_TRIM_PAYLOAD` at the top of the file (currently lines ~59-60). Keep other imports from `kiro.config` intact.

(b) Add a fallback module-level read (NOT a value import) so the builder can be called without explicit settings (e.g., from older code paths or tests):

```python
def _resolve_payload_settings(settings: Optional[Dict[str, Any]]) -> Tuple[int, bool]:
    """Return (max_bytes, auto_trim) — explicit settings win, else env defaults."""
    if settings is not None:
        return int(settings.get("max_bytes", 600_000)), bool(settings.get("auto_trim", True))
    from kiro.config import KIRO_MAX_PAYLOAD_BYTES, AUTO_TRIM_PAYLOAD
    return int(KIRO_MAX_PAYLOAD_BYTES), bool(AUTO_TRIM_PAYLOAD)
```

(c) Modify `build_kiro_payload` signature to accept the new keyword-only parameter:

```python
def build_kiro_payload(
    messages: List[UnifiedMessage],
    system_prompt: str,
    model_id: str,
    tools: Optional[List[UnifiedTool]],
    conversation_id: str,
    profile_arn: str,
    thinking_config: ThinkingConfig,
    *,
    monitor_request_id: Optional[str] = None,
    payload_settings: Optional[Dict[str, Any]] = None,
) -> KiroPayloadResult:
```

(d) Inside the function body, replace the existing trim block (currently lines 1606-1626):

```python
    # Payload size guard — auto-trim if enabled (settings can come from runtime control panel)
    max_bytes, auto_trim = _resolve_payload_settings(payload_settings)
    raw_size = check_payload_size(payload)
    if auto_trim and raw_size > max_bytes:
        stats = trim_payload_to_limit(payload, max_bytes)
        logger.info(
            f"Trimmed conversation history: {stats.original_entries} -> {stats.final_entries} messages "
            f"({stats.original_bytes} -> {stats.final_bytes} bytes)"
        )
        try:
            from kiro.control_panel import control_panel as _cp
            if monitor_request_id:
                _cp.record_trim(
                    monitor_request_id,
                    before=stats.original_entries,
                    after=stats.final_entries,
                    before_bytes=stats.original_bytes,
                    after_bytes=stats.final_bytes,
                )
        except Exception:
            pass

    # Record the actual size sent upstream (always, regardless of trim)
    try:
        from kiro.control_panel import control_panel as _cp2
        if monitor_request_id:
            final_size = check_payload_size(payload)
            _cp2.record_request_bytes(monitor_request_id, final_size)
    except Exception:
        pass
```

(e) Make sure `Tuple` is imported from `typing` (it likely already is — grep `from typing import` near the top).

- [ ] **Step 7: Forward parameter through Anthropic + OpenAI adapters**

In `kiro/converters_anthropic.py`, find `def anthropic_to_kiro(` (line ~429). Add `payload_settings: Optional[Dict[str, Any]] = None` to the signature (keyword-only after the `*` if there is one, else as a new kwarg with default `None`). Then in the body where `build_kiro_payload(...)` is called (line ~479), add `payload_settings=payload_settings` to the call.

In `kiro/converters_openai.py`, locate the analogous wrapper function (grep `build_kiro_payload`). Do the same threading.

- [ ] **Step 8: Update route call sites**

In `kiro/routes_openai.py`, locate every call to `build_kiro_payload(` (grep — there are several). Before each call, fetch settings:

```python
                payload_settings = request.app.state.control_panel.get_payload_settings()
                kiro_payload = build_kiro_payload(
                    ...existing args...,
                    monitor_request_id=monitor_request_id,
                    payload_settings=payload_settings,
                )
```

In `kiro/routes_anthropic.py`, locate calls to `anthropic_to_kiro(` and add `payload_settings=request.app.state.control_panel.get_payload_settings()` to each.

- [ ] **Step 9: Run regression + import check**

```bash
cd /Users/jamexhuang/Documents/github/kiro-gateway
python -c "import kiro.routes_anthropic; import kiro.routes_openai; import kiro.converters_core; print('OK')"
pytest tests/unit/test_account_manager.py tests/unit/test_control_panel.py -q
```

Expected: import OK; all existing tests pass.

- [ ] **Step 10: Commit**

```bash
rtk git add kiro/control_panel.py kiro/converters_core.py kiro/converters_anthropic.py kiro/converters_openai.py kiro/routes_openai.py kiro/routes_anthropic.py tests/unit/test_control_panel.py
rtk git commit -m "feat(payload): inject runtime settings into builder + record_request_bytes"
```

---

## Task 3: Cumulative usage metrics + completion hook + dashboard route

**Files:**
- Modify: `kiro/metrics.py` (extend `MetricsRegistry` OR add a new class — pick a separate `UsageStatsRegistry` to keep the rolling window logic clean)
- Modify: `kiro/control_panel.py` (call `usage_stats_registry.record_completion` in the completion site)
- Modify: `kiro/routes_dashboard.py` (add `/dashboard/api/totals`)
- Create: `tests/unit/test_metrics.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_metrics.py`:

```python
"""Tests for cumulative usage statistics."""
import pytest

from kiro.metrics import UsageStatsRegistry


class TestUsageStatsRegistry:
    def test_initial_snapshot_is_empty(self):
        r = UsageStatsRegistry()
        snap = r.snapshot()
        assert snap["total"] == {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "payload_bytes": 0,
        }
        assert snap["by_model"] == {}
        assert "since" in snap

    def test_record_completion_increments_totals(self):
        r = UsageStatsRegistry()
        r.record_completion("claude-opus-4.5", input_tokens=100, output_tokens=50, payload_bytes=2048)
        r.record_completion("claude-opus-4.5", input_tokens=200, output_tokens=80, payload_bytes=4096)
        snap = r.snapshot()
        assert snap["total"]["requests"] == 2
        assert snap["total"]["input_tokens"] == 300
        assert snap["total"]["output_tokens"] == 130
        assert snap["total"]["payload_bytes"] == 6144

    def test_record_completion_accumulates_by_model(self):
        r = UsageStatsRegistry()
        r.record_completion("claude-opus-4.5", 100, 50, 1000)
        r.record_completion("claude-haiku-4.5", 200, 80, 2000)
        r.record_completion("claude-opus-4.5", 300, 120, 3000)
        snap = r.snapshot()
        by_m = snap["by_model"]
        assert by_m["claude-opus-4.5"]["requests"] == 2
        assert by_m["claude-opus-4.5"]["input_tokens"] == 400
        assert by_m["claude-opus-4.5"]["output_tokens"] == 170
        assert by_m["claude-opus-4.5"]["payload_bytes"] == 4000
        assert by_m["claude-haiku-4.5"]["requests"] == 1
        assert by_m["claude-haiku-4.5"]["input_tokens"] == 200

    def test_record_completion_treats_none_as_zero(self):
        r = UsageStatsRegistry()
        r.record_completion("claude-opus-4.5", None, None, None)
        snap = r.snapshot()
        assert snap["total"]["requests"] == 1
        assert snap["total"]["input_tokens"] == 0
        assert snap["total"]["output_tokens"] == 0
        assert snap["total"]["payload_bytes"] == 0

    def test_record_completion_skips_empty_model(self):
        """An empty or None model name should still count the request but in a fallback bucket."""
        r = UsageStatsRegistry()
        r.record_completion("", 10, 20, 30)
        snap = r.snapshot()
        assert snap["total"]["requests"] == 1
        # Empty model lands in a sentinel bucket
        assert "(unknown)" in snap["by_model"]
        assert snap["by_model"]["(unknown)"]["requests"] == 1
```

- [ ] **Step 2: Run tests to confirm failure**

Run: `cd /Users/jamexhuang/Documents/github/kiro-gateway && pytest tests/unit/test_metrics.py -v`
Expected: ImportError no `UsageStatsRegistry`.

- [ ] **Step 3: Implement `UsageStatsRegistry` in `kiro/metrics.py`**

Append to `kiro/metrics.py`:

```python
import time as _time
from typing import Optional


class UsageStatsRegistry:
    """Process-lifetime cumulative usage counters with per-model breakdown.

    Session-scoped (resets on process restart). All increments under a lock for thread safety.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._since: float = _time.time()
        self._total_requests: int = 0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._total_payload_bytes: int = 0
        self._by_model: Dict[str, Dict[str, int]] = {}

    def record_completion(
        self,
        model: Optional[str],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        payload_bytes: Optional[int],
    ) -> None:
        """Increment cumulative counters. None values are treated as 0."""
        i = int(input_tokens) if input_tokens else 0
        o = int(output_tokens) if output_tokens else 0
        b = int(payload_bytes) if payload_bytes else 0
        key = model if model else "(unknown)"
        with self._lock:
            self._total_requests += 1
            self._total_input_tokens += i
            self._total_output_tokens += o
            self._total_payload_bytes += b
            bucket = self._by_model.setdefault(
                key, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "payload_bytes": 0}
            )
            bucket["requests"] += 1
            bucket["input_tokens"] += i
            bucket["output_tokens"] += o
            bucket["payload_bytes"] += b

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "since": self._since,
                "total": {
                    "requests": self._total_requests,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                    "payload_bytes": self._total_payload_bytes,
                },
                "by_model": {k: dict(v) for k, v in self._by_model.items()},
            }


usage_stats_registry = UsageStatsRegistry()
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd /Users/jamexhuang/Documents/github/kiro-gateway && pytest tests/unit/test_metrics.py -v`
Expected: 5 passed.

- [ ] **Step 5: Hook into request completion site**

In `kiro/control_panel.py`, find the existing `complete_request` (or equivalent) method — the one that calls `metrics_registry.record(now, duration, is_error)` (currently around line 855). Add immediately AFTER that block, INSIDE the same `try/except`:

```python
            try:
                from kiro.metrics import usage_stats_registry
                if status == "completed":  # only count successful completions
                    usage_stats_registry.record_completion(
                        model=record.active_model,
                        input_tokens=record.input_tokens,
                        output_tokens=record.output_tokens,
                        payload_bytes=record.request_bytes,
                    )
            except Exception:
                pass
```

Place this as a SEPARATE `try/except` block right after the existing `metrics_registry.record(...)` block (do not merge — keep them isolated so one failure doesn't mask the other).

- [ ] **Step 6: Add dashboard `/dashboard/api/totals` route**

In `kiro/routes_dashboard.py`, right after the `set_payload_settings` endpoint from Task 1, add:

```python
@router.get("/dashboard/api/totals", dependencies=[Security(verify_dashboard_api_key)])
async def get_totals() -> Dict[str, Any]:
    """Return cumulative usage totals + per-model breakdown for this process."""
    from kiro.metrics import usage_stats_registry
    return usage_stats_registry.snapshot()
```

- [ ] **Step 7: Run all related tests**

```bash
cd /Users/jamexhuang/Documents/github/kiro-gateway
pytest tests/unit/test_metrics.py tests/unit/test_control_panel.py tests/unit/test_account_manager.py -q
python -c "import kiro.routes_dashboard; print('OK')"
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
rtk git add kiro/metrics.py kiro/control_panel.py kiro/routes_dashboard.py tests/unit/test_metrics.py
rtk git commit -m "feat(metrics): cumulative usage stats + completion hook + /totals route"
```

---

## Task 4: Dashboard UI A — payload widget + per-request columns

**Files:**
- Modify: `kiro/routes_dashboard.py` (HTML/JS only)

- [ ] **Step 1: Add payload settings widget to `#panel-accounts` header**

Find the existing accounts panel `<header>` (the one modified in the previous round-robin work, around line 807). Add a NEW `<div>` immediately AFTER the closing `</header>`, before the description `<p>`. The new block:

```html
      <div class="payload-settings" style="margin-bottom:10px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="color:var(--muted)">Payload 上限：</span>
        <input id="payloadMaxBytes" type="range" min="50000" max="2000000" step="10000" value="600000" style="flex:1;min-width:160px;max-width:320px">
        <span id="payloadMaxBytesLabel" class="tag" style="font-size:11px;min-width:80px;text-align:right">600 KB</span>
        <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer"><input id="payloadAutoTrim" type="checkbox"> 自動修剪</label>
        <button class="btn" onclick="applyPayloadSettings()" style="font-size:11px;padding:4px 10px">套用</button>
        <span id="payloadSaveResult" style="font-size:11px;color:var(--muted)"></span>
      </div>
```

- [ ] **Step 2: Update the requests table header + row renderer**

Find `renderRequests` (around line 1025) and the `reqRow` helper (around line 1009).

Replace the `<thead>` line inside `renderRequests` with one that has new columns:

```html
    <thead><tr><th>開始</th><th>狀態</th><th>格式</th><th>模型</th><th>總耗時</th><th>首字</th><th>速度</th><th>in/out</th><th>重試</th><th>裁剪</th><th>payload</th><th>錯誤</th></tr></thead>
```

Update `reqRow`. Find the existing line that renders the `token` column:

```javascript
    <td>${ttft}</td><td>${tps}</td><td>${r.output_tokens??"—"}</td><td>${attempts}</td><td>${esc(trim)}</td>
```

Replace with:

```javascript
    <td>${ttft}</td><td>${tps}</td><td>${(r.input_tokens??"—")} / ${(r.output_tokens??"—")}</td><td>${attempts}</td><td>${esc(trim)}</td><td>${r.request_bytes!=null?fmtBytes(r.request_bytes):"—"}</td>
```

- [ ] **Step 3: Add `fmtBytes` helper near `fmtMs` / `fmtTime`**

Find `function fmtMs` (search the JS section). Right after it, add:

```javascript
function fmtBytes(n){if(n==null)return "—";if(n<1024)return n+" B";if(n<1024*1024)return (n/1024).toFixed(1)+" KB";return (n/1024/1024).toFixed(2)+" MB";}
```

- [ ] **Step 4: Add `initPayloadSettings` / `applyPayloadSettings` JS**

Right after the `initAccountStrategy` JS block (added in the previous task, around line 1228), insert:

```javascript
// -------- payload settings --------
async function initPayloadSettings(){
  try{
    const d=await api("/dashboard/api/payload-settings");
    $("#payloadMaxBytes").value=d.max_bytes;
    $("#payloadMaxBytesLabel").textContent=fmtBytes(d.max_bytes);
    $("#payloadAutoTrim").checked=!!d.auto_trim;
  }catch{}
}
$("#payloadMaxBytes").addEventListener("input",()=>{
  $("#payloadMaxBytesLabel").textContent=fmtBytes(parseInt($("#payloadMaxBytes").value,10));
});
async function applyPayloadSettings(){
  const max_bytes=parseInt($("#payloadMaxBytes").value,10);
  const auto_trim=$("#payloadAutoTrim").checked;
  try{
    const d=await api("/dashboard/api/payload-settings",{method:"PUT",body:JSON.stringify({max_bytes,auto_trim})});
    $("#payloadMaxBytes").value=d.max_bytes;
    $("#payloadMaxBytesLabel").textContent=fmtBytes(d.max_bytes);
    $("#payloadAutoTrim").checked=!!d.auto_trim;
    $("#payloadSaveResult").textContent="已套用 "+new Date().toLocaleTimeString();
  }catch(e){
    $("#payloadSaveResult").textContent=e.message||"套用失敗";
    initPayloadSettings();  // revert to server state
  }
}
```

- [ ] **Step 5: Wire `initPayloadSettings()` into `connect()`**

Find `connect()` (around line 870-879). After `initAccountStrategy();` add:

```javascript
  initPayloadSettings();
```

- [ ] **Step 6: Smoke-import test**

Run: `cd /Users/jamexhuang/Documents/github/kiro-gateway && python -c "import kiro.routes_dashboard; print('OK')"`
Expected: OK.

- [ ] **Step 7: Verify the new symbols are present**

```bash
grep -nE 'payloadMaxBytes|initPayloadSettings|applyPayloadSettings|fmtBytes|"payload"' kiro/routes_dashboard.py | head -20
```

Expected: shows the new HTML/JS additions.

- [ ] **Step 8: Commit**

```bash
rtk git add kiro/routes_dashboard.py
rtk git commit -m "feat(dashboard): payload-size widget + per-request in/out tokens + payload column"
```

---

## Task 5: Dashboard UI B — 累計用量 panel

**Files:**
- Modify: `kiro/routes_dashboard.py` (HTML/JS only)

- [ ] **Step 1: Add nav entry and panel section**

Find the nav `<a data-jump=...>` list (around line 691-695). Insert a new entry between `帳號` and `延遲分析`:

```html
        <a data-jump="panel-totals">累計用量</a>
```

Find the `</section>` closing tag of `#panel-accounts` (just after the accounts div, around line 775). Insert the new panel BEFORE the next section:

```html
    <section id="panel-totals" class="panel">
      <header>
        <h2>累計用量</h2>
        <span id="totalsSince" class="tag" style="font-size:11px;color:var(--muted)">—</span>
      </header>
      <p style="font-size:11px;color:var(--muted);margin-bottom:8px">本進程啟動至今的累計使用量。重啟後歸零（不持久化）。</p>
      <div id="totalsCards" style="display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:8px;margin-bottom:10px"></div>
      <div id="totalsByModel"><p style="color:var(--muted)">等待資料…</p></div>
    </section>
```

- [ ] **Step 2: Add JS pull + render**

After the `initPayloadSettings` JS block, insert:

```javascript
// -------- usage totals --------
async function pullTotals(){
  try{const d=await api("/dashboard/api/totals");state.totals=d;renderTotals();}catch{}
}
function renderTotals(){
  const d=state.totals;if(!d)return;
  const since=new Date(d.since*1000);
  $("#totalsSince").textContent="since "+since.toLocaleString();
  const t=d.total;
  $("#totalsCards").innerHTML=[
    {label:"總請求",value:t.requests.toLocaleString()},
    {label:"Input tokens",value:fmtNum(t.input_tokens)},
    {label:"Output tokens",value:fmtNum(t.output_tokens)},
    {label:"Payload",value:fmtBytes(t.payload_bytes)},
  ].map(c=>`<div style="border:1px solid var(--border);border-radius:6px;padding:8px 10px"><div style="font-size:10.5px;color:var(--muted)">${c.label}</div><div style="font-size:18px;font-weight:600;margin-top:2px">${c.value}</div></div>`).join("");
  const models=Object.entries(d.by_model||{}).map(([m,v])=>({m,...v})).sort((a,b)=>(b.input_tokens+b.output_tokens)-(a.input_tokens+a.output_tokens));
  if(!models.length){$("#totalsByModel").innerHTML=`<p style="color:var(--muted)">尚無資料。</p>`;return;}
  $("#totalsByModel").innerHTML=`<table class="reqs"><thead><tr><th>模型</th><th>請求數</th><th>input</th><th>output</th><th>payload</th></tr></thead><tbody>${
    models.map(r=>`<tr><td>${esc(r.m)}</td><td>${r.requests}</td><td>${fmtNum(r.input_tokens)}</td><td>${fmtNum(r.output_tokens)}</td><td>${fmtBytes(r.payload_bytes)}</td></tr>`).join("")
  }</tbody></table>`;
}
function fmtNum(n){if(n<1000)return String(n);if(n<1_000_000)return (n/1000).toFixed(1)+"k";if(n<1_000_000_000)return (n/1_000_000).toFixed(2)+"M";return (n/1_000_000_000).toFixed(2)+"G";}
```

- [ ] **Step 3: Wire pull into `connect()` polling loop**

Find `pullMetrics();setInterval(pullMetrics,5000);` (around line 876). Add immediately after:

```javascript
  pullTotals();setInterval(pullTotals,5000);
```

- [ ] **Step 4: Smoke import + grep verify**

```bash
cd /Users/jamexhuang/Documents/github/kiro-gateway
python -c "import kiro.routes_dashboard; print('OK')"
grep -nE 'panel-totals|pullTotals|renderTotals|fmtNum' kiro/routes_dashboard.py | head -10
```

Expected: import OK; greps show the new symbols.

- [ ] **Step 5: Run the full unit suite for sanity**

```bash
cd /Users/jamexhuang/Documents/github/kiro-gateway
pytest tests/unit/test_account_manager.py tests/unit/test_control_panel.py tests/unit/test_metrics.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
rtk git add kiro/routes_dashboard.py
rtk git commit -m "feat(dashboard): 累計用量 panel with totals + per-model breakdown"
```

---

## Self-review notes

- **Spec coverage**: A1→Task 1, A2→Task 2, A3→Task 1, A4→Task 4, A5→Task 4, A6→Task 2, B1→Task 3, B2→Task 3, B3→Task 3, B4→Task 5. All ten components mapped.
- **No placeholders**: every step contains exact code or commands.
- **Type consistency**: `payload_settings: Optional[Dict[str, Any]]` used consistently from `build_kiro_payload` through `anthropic_to_kiro` and route handlers. `request_bytes` field name is identical in `RequestRecord`, `record_request_bytes`, and `record_completion` argument. `record_completion` argument order: `model, input_tokens, output_tokens, payload_bytes`.
- **In-flight safety**: Tasks 1+2 change settings via setter that only affects new builder calls; in-progress requests' payload has already been built and sent. Task 3 totals are read-only side effect. Task 4/5 are UI only.
- **Persistence**: routing_state.json schema gets a new `"payload"` key; missing key falls back to env defaults — backwards compatible.
