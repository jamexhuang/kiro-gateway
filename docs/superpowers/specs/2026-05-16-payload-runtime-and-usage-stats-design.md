# Payload Runtime Settings + Cumulative Usage Stats — Design

## Goal

Give operators two new dashboard capabilities without touching in-flight connections:

1. **Sub-spec A — Payload runtime controls + per-request size/token display.** Adjust `KIRO_MAX_PAYLOAD_BYTES` and `AUTO_TRIM_PAYLOAD` from the UI; show per-request input tokens and payload bytes in the requests table.
2. **Sub-spec B — Cumulative usage stats panel.** Aggregate `input_tokens` / `output_tokens` / `payload_bytes` across the process lifetime, grouped by model, surfaced in a new panel.

**Out of scope (explicitly dropped after spike).** Region/endpoint switching. The 2026-05-16 spike against `q.{region}.amazonaws.com` confirmed only `us-east-1` (native) responds 200; `eu-central-1` resolves DNS but returns HTTP 400; every other documented Kiro region (`us-east-2`, `us-west-2`, `eu-west-*`, `eu-north-1`, `eu-south-*`) has no hostname. Documentation describes Bedrock-internal cross-region inference, not client-facing endpoints. Full results at [debug_logs/region_spike_result.json](../../../debug_logs/region_spike_result.json).

## Architecture

Both features share a single principle: **the runtime control plane reads/writes through `ControlPanelState` and `MetricsRegistry`; the request path reads from these at the moment of need, never at import.** This is how the just-shipped `ACCOUNT_STRATEGY` runtime toggle works, and the same invariant guarantees in-flight requests are unaffected by toggle changes.

```
┌────────────────────┐     PUT/GET      ┌──────────────────────┐
│ dashboard (HTML/JS)│ <──────────────► │ /dashboard/api/...   │
└────────────────────┘                  └──────────┬───────────┘
                                                   │
                              ┌────────────────────┼──────────────────────┐
                              ▼                    ▼                      ▼
                  ┌──────────────────────┐  ┌──────────────────┐  ┌──────────────┐
                  │ ControlPanelState    │  │ MetricsRegistry  │  │ AccountMgr   │
                  │  • payload_settings  │  │  • cumulative    │  │ (no change)  │
                  │  • routing (existing)│  │  • by_model      │  │              │
                  └──────────┬───────────┘  └────────┬─────────┘  └──────────────┘
                             │ read at request time           ▲
                  ┌──────────▼────────────┐                   │
                  │ payload_guards (read) │     record_completion() on done
                  └───────────────────────┘                   │
                                                              │
                                          ┌───────────────────┘
                                          │
                                ┌─────────┴─────────┐
                                │ routes_anthropic, │
                                │ routes_openai     │
                                └───────────────────┘
```

## Components

### Sub-spec A: Payload runtime settings

#### A1. `ControlPanelState` payload settings (file: `kiro/control_panel.py`)

- Add fields seeded from `kiro.config.KIRO_MAX_PAYLOAD_BYTES` / `AUTO_TRIM_PAYLOAD` on construction.
- Public API:
  - `get_payload_settings() -> {"max_bytes": int, "auto_trim": bool}`
  - `set_payload_settings(max_bytes: int, auto_trim: bool) -> None` — validates `50_000 <= max_bytes <= 2_000_000`, raises `ValueError` otherwise.
- Persist alongside routing state in the existing `routing_state.json` (extend its JSON object; missing fields fall back to env defaults).
- All mutation goes through the existing `ControlPanelState` lock.

#### A2. Payload guard reads runtime value (file: `kiro/payload_guards.py`)

- Remove value-imports `from kiro.config import KIRO_MAX_PAYLOAD_BYTES, AUTO_TRIM_PAYLOAD` at module top.
- Change the guard function signature to take a `settings: dict` parameter (shape `{"max_bytes": int, "auto_trim": bool}`).
- At every call site (grep `kiro.payload_guards` to find them), the caller — which has access to `request.app.state.control_panel` — pulls the live settings via `control_panel.get_payload_settings()` and passes them in.
- Rationale: explicit dependency injection > hidden module singleton; mirrors how `app.state.account_manager` is plumbed.

#### A3. Dashboard routes (file: `kiro/routes_dashboard.py`)

- `GET /dashboard/api/payload-settings` — returns `{"max_bytes": int, "auto_trim": bool}`.
- `PUT /dashboard/api/payload-settings` — body `{"max_bytes": int, "auto_trim": bool}`; validates ranges; on failure returns HTTP 400 with reason.
- Both protected by `Security(verify_dashboard_api_key)`.

#### A4. Dashboard UI: payload settings widget

- Compact sub-section directly below the accounts panel header (before account cards), inside `#panel-accounts`:
  - Range slider `50_000 – 2_000_000` step `10_000`, with live numeric label.
  - Checkbox "自動修剪超量 payload".
  - Apply button (PUT on click), success/error chip.
- JS: `initPayloadSettings()` fetches state on connect, on Apply PUTs and updates label; failure shows `alert` and reverts the slider/checkbox to last-known good.

#### A5. Per-request token/payload columns

- Requests table currently shows: 開始 / 狀態 / 格式 / 模型 / 總耗時 / 首字 / 速度 / token / 重試 / 裁剪 / 錯誤.
- Replace single `token` column with `in/out` (e.g., `1.2k / 380`).
- Insert new `payload` column between `裁剪` and `錯誤`, formatted human-readable (`123 KB`, `1.4 MB`); reads a new `RequestRecord.request_bytes` field (introduced in A6 below) that is set unconditionally at upstream send time. Falls back to `—` if the request errored before send.

#### A6. `RequestRecord.request_bytes` (file: `kiro/control_panel.py`)

- Add a new optional field `request_bytes: Optional[int] = None` to `RequestRecord`.
- Set it at the same call site that currently captures `trim_before_bytes` — but unconditionally, regardless of whether the trim guard fires. This becomes the canonical "size sent upstream" value used by both the per-request column (A5) and the cumulative bytes counter (B2).
- `trim_before_bytes` / `trim_after_bytes` retain their existing semantics (only set when trim runs).

### Sub-spec B: Cumulative usage stats

#### B1. `MetricsRegistry` cumulative counters (file: `kiro/metrics.py`)

- Add to the registry:
  - `total_requests: int`
  - `total_input_tokens: int`
  - `total_output_tokens: int`
  - `total_payload_bytes: int`
  - `by_model: dict[str, {requests, input_tokens, output_tokens, payload_bytes}]`
  - `since: float` (process start epoch — already available, just expose)
- New method `record_completion(model: str, input_tokens: int, output_tokens: int, payload_bytes: int)` — atomic increment under existing lock; treats `None` as `0`.
- Counters are session-only (reset on process restart). No persistence in this iteration; can revisit if the user needs historical aggregation.

#### B2. Hook record_completion into request finalisation (file: `kiro/control_panel.py`)

- Find the existing completion site (where `output_tokens` is set on the `RequestRecord` and the record transitions to `completed`) and add a call: `metrics_registry.record_completion(record.active_model, record.input_tokens or 0, record.output_tokens or 0, record.request_bytes or 0)`.
- Only fire on terminal `completed` status (skip `client_disconnected` and `error` to avoid skewing the totals — those are visible separately in the requests table).

#### B3. Dashboard route (file: `kiro/routes_dashboard.py`)

- `GET /dashboard/api/totals` — returns `{"total": {...}, "by_model": {...}, "since": "ISO8601"}`.
- Protected by `Security(verify_dashboard_api_key)`.

#### B4. Dashboard UI: 累計用量 panel

- New nav entry `<a data-jump="panel-totals">累計用量</a>` added between `帳號` and `延遲分析`.
- Panel structure:
  - Header with "累計用量" title and a small "since 14:48:21" tag (relative to process start).
  - Four cards top-row: 總請求 / Input tokens / Output tokens / Payload bytes (human-readable).
  - Below: table — 模型 | 請求數 | input | output | payload — sorted by `input + output` desc, fixed-height scroll.
- JS: `pullTotals()` polls `/dashboard/api/totals` every 5s (matches existing `pullMetrics`); `renderTotals()` formats numbers (k/M/G suffix) and percentages.

## Data flow

Per-request flow (unchanged ingress, three new touch points):

1. Request arrives → routes_{anthropic,openai} build payload.
2. **Touch point 1**: `payload_guards.maybe_trim(...)` reads live `max_bytes` / `auto_trim` from `control_panel.get_payload_settings()`. If a setter call landed between steps 1 and 2, the new value wins; if mid-step, it doesn't (no half-applied guard).
3. Upstream call fires, streams.
4. On completion, `RequestRecord` is updated with `output_tokens`, `input_tokens`, `trim_*_bytes`.
5. **Touch point 2**: `record_completion(...)` fires, increments totals.
6. **Touch point 3**: dashboard SSE pushes the record (existing); next `/dashboard/api/totals` poll surfaces the updated totals.

## Error handling

- `PUT /dashboard/api/payload-settings` with out-of-range bytes → 400 + descriptive message; UI alerts and reverts.
- `record_completion` is best-effort: an exception inside it must not propagate to the request response. Wrap in `try/except` and log a warning.
- Polling endpoint failures: existing `api()` JS helper already shows transient errors; no change.
- Concurrency: all `MetricsRegistry` mutations under the existing lock; all `ControlPanelState` mutations under its lock.

## Testing

| File | New tests |
|---|---|
| `tests/unit/test_control_panel.py` | get/set payload settings; persistence round-trip via `routing_state.json`; range validation rejects values outside `[50_000, 2_000_000]` |
| `tests/unit/test_metrics.py` (new file if absent, else extend) | `record_completion` increments all counters; `by_model` accumulates per model; None-safe inputs; snapshot endpoint returns expected shape |
| `tests/unit/test_routes_openai.py` *or* an integration test | One round-trip request with mocked Kiro response — assert `total_input_tokens` / `total_output_tokens` move by the expected deltas |

Each new test follows TDD: write red, implement, watch green, commit.

## Migration notes

- Existing `routing_state.json` files without payload-settings fields fall back to env defaults — fully backwards compatible.
- Existing `state.json` is untouched.
- Existing env vars (`KIRO_MAX_PAYLOAD_BYTES`, `AUTO_TRIM_PAYLOAD`) remain the bootstrap source; on first start they seed the control panel, and the saved values then take precedence on restart.

## Risks

- **Payload guard call sites are scattered.** Implementer should grep `KIRO_MAX_PAYLOAD_BYTES` and `AUTO_TRIM_PAYLOAD` and confirm every read uses the runtime accessor; missing a site leaves a stale value in the hot path.
- **Metrics ring-buffer interaction.** Existing `MetricsRegistry` has a time-bucketed window for RPS/P50/P95; cumulative counters are separate (lifetime totals) and must not be confused with that window.
- **None-safety on `input_tokens`.** Anthropic responses sometimes omit usage on early errors; `record_completion` must treat missing values as 0 not crash.
