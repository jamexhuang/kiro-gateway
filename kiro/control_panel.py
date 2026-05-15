# -*- coding: utf-8 -*-

"""
Runtime control panel state for model routing and request monitoring.

This module intentionally keeps all state in memory and starts no background
tasks. Existing API traffic only pays for a small routing decision and optional
monitoring writes while the server is already handling a request.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from threading import RLock
from typing import Any, Callable, Deque, Dict, List, Optional

from loguru import logger

from kiro.config import MODEL_FALLBACKS
from kiro.model_resolver import normalize_model_name
from kiro.config import (
    MAX_CONCURRENT_UPSTREAM_REQUESTS,
    REQUEST_JITTER_MS,
    THROTTLE_FAST_FAIL,
)
from kiro.latency_tracer import LatencyTracer, get_tracer, is_enabled as latency_enabled


ROUTING_MODES = {"passthrough", "manual", "redirect"}
MODEL_FAILURE_STATUS_CODES = {400, 403, 404, 422, 429, 502, 504}
MODEL_QUALITY_ORDER = {"haiku": 1, "sonnet": 2, "opus": 3}


@dataclass
class ThrottleConfig:
    """Runtime burst protection configuration."""

    max_concurrent: int = MAX_CONCURRENT_UPSTREAM_REQUESTS
    jitter_ms: int = REQUEST_JITTER_MS
    throttle_fast_fail: bool = THROTTLE_FAST_FAIL
    enabled: bool = True


@dataclass
class RoutingConfig:
    """
    Runtime model routing configuration.

    Attributes:
        enabled: Whether runtime routing can change requested models.
        mode: Routing mode. passthrough keeps the client model, manual forces
            one model, redirect applies per-model mappings.
        manual_model: Model used when mode is manual.
        redirects: Exact or normalized model-to-model redirect rules.
        fallback_enabled: Whether configured fallback models are used after a
            model-level failure.
        fallback_models: Models tried after the first attempt fails.
        safe_fallback_to_original: Retry the original client model first when
            a dashboard rewrite fails.
        capture_content: Store request/response payload excerpts in memory.
        max_content_chars: Max characters stored per captured payload/chunk.
    """

    enabled: bool = False
    mode: str = "passthrough"
    manual_model: str = "claude-opus-4.6"
    redirects: Dict[str, str] = field(
        default_factory=lambda: {"claude-opus-4.7": "claude-opus-4.6"}
    )
    fallback_enabled: bool = True
    fallback_models: List[str] = field(
        default_factory=lambda: MODEL_FALLBACKS.get("claude-opus-4.7", ["claude-opus-4.6"])
    )
    safe_fallback_to_original: bool = True
    capture_content: bool = False
    max_content_chars: int = 12000


@dataclass(frozen=True)
class RoutingDecision:
    """
    Result of applying runtime routing to a client model.

    Attributes:
        original_model: Model requested by the client.
        routed_model: Model that should be sent on the first attempt.
        applied: True when runtime routing changed the model.
        mode: Routing mode that produced the decision.
        reason: Human-readable reason for dashboards and logs.
        fallback_models: Ordered fallback models to try after first failure.
    """

    original_model: str
    routed_model: str
    applied: bool
    mode: str
    reason: str
    fallback_models: List[str]


@dataclass
class RequestAttempt:
    """
    One upstream Kiro attempt for a proxied request.

    Attributes:
        model: Client-facing model name used for this attempt.
        account_id: Account used for this attempt, if available.
        status: Current attempt status.
        started_at: UNIX timestamp when this attempt started.
        finished_at: UNIX timestamp when this attempt finished.
        http_status: Upstream HTTP status code, if available.
        error: Error message, if available.
    """

    model: str
    account_id: Optional[str]
    status: str
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    http_status: Optional[int] = None
    error: Optional[str] = None


@dataclass
class RequestRecord:
    """
    Request monitoring record.

    Attributes:
        id: Unique request ID for the dashboard.
        api_format: API surface, either openai or anthropic.
        path: HTTP path.
        stream: Whether the client requested streaming.
        original_model: Model requested by the client.
        routed_model: First model selected by runtime routing.
        active_model: Model currently being attempted.
        routing_reason: Explanation of routing behavior.
        status: Request status shown on the dashboard.
        started_at: UNIX timestamp when the request started.
        updated_at: UNIX timestamp when the record last changed.
        ended_at: UNIX timestamp when the request ended.
        attempts: Upstream attempts made for this request.
        payloads: Captured request payload excerpts.
        chunks: Captured response stream excerpts.
        response: Captured non-stream response excerpt.
        error: Request error, if any.
    """

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
    # Per-stage latency breakdown (populated when LATENCY_TRACING is enabled).
    trace: Optional[Dict[str, Any]] = None


def _dedupe_models(models: List[str]) -> List[str]:
    """
    Remove empty and duplicate model names while preserving order.

    Args:
        models: Candidate model names.

    Returns:
        Ordered list with duplicates removed.
    """
    seen = set()
    result = []
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def _get_model_quality(model: str) -> Optional[int]:
    """
    Return the known Claude model quality tier.

    Args:
        model: Model name.

    Returns:
        Numeric quality tier, or None for unknown model families.
    """
    normalized = normalize_model_name(model).lower()
    for family, tier in MODEL_QUALITY_ORDER.items():
        if family in normalized:
            return tier
    return None


def _is_quality_downgrade(primary_model: str, fallback_model: str) -> bool:
    """
    Decide whether fallback would automatically downgrade model quality.

    Args:
        primary_model: Model used for the current attempt.
        fallback_model: Candidate fallback model.

    Returns:
        True when fallback would move to a lower known model family.
    """
    primary_quality = _get_model_quality(primary_model)
    fallback_quality = _get_model_quality(fallback_model)
    if primary_quality is None or fallback_quality is None:
        return False
    return fallback_quality < primary_quality


def _serialize_excerpt(value: Any, max_chars: int) -> str:
    """
    Convert arbitrary content to a bounded string for in-memory monitoring.

    Args:
        value: Value to serialize.
        max_chars: Maximum number of characters to retain.

    Returns:
        Serialized excerpt.
    """
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(value)

    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n...[truncated {len(text) - max_chars} chars]"


class ControlPanelState:
    """
    In-memory control panel state with optional routing persistence.

    The class is thread-safe for FastAPI's mixed sync/async access pattern.
    Routing config is persisted to disk so dashboard settings survive restarts.
    """

    ROUTING_STATE_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routing_state.json"
    )

    def __init__(self, max_completed_requests: int = 1000, max_chunks_per_request: int = 80, persist: bool = True):
        """
        Initialize the control panel state.

        Args:
            max_completed_requests: Number of completed records to keep.
            max_chunks_per_request: Number of stream chunks to keep per request.
            persist: Whether to load/save routing state from disk.
        """
        self._lock = RLock()
        self._routing = RoutingConfig()
        self._throttle = ThrottleConfig()
        self._active_requests: Dict[str, RequestRecord] = {}
        self._completed_requests: Deque[RequestRecord] = deque(maxlen=max_completed_requests)
        self._max_chunks_per_request = max_chunks_per_request
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []
        # Per-request latency tracers (only populated when LATENCY_TRACING enabled)
        self._tracers: Dict[str, LatencyTracer] = {}
        self._persist = persist
        if persist:
            self._load_routing_state()

    def _load_routing_state(self) -> None:
        """Load persisted routing config from disk if available."""
        try:
            if os.path.exists(self.ROUTING_STATE_FILE):
                with open(self.ROUTING_STATE_FILE, "r") as f:
                    data = json.load(f)
                routing_data = data.get("routing")
                if routing_data:
                    allowed = set(RoutingConfig.__dataclass_fields__.keys())
                    filtered = {k: v for k, v in routing_data.items() if k in allowed}
                    self._routing = RoutingConfig(**filtered)
                    logger.info(f"Loaded persisted routing: enabled={self._routing.enabled}, mode={self._routing.mode}")
                throttle_data = data.get("throttle")
                if throttle_data:
                    allowed = set(ThrottleConfig.__dataclass_fields__.keys())
                    filtered = {k: v for k, v in throttle_data.items() if k in allowed}
                    self._throttle = ThrottleConfig(**filtered)
                    logger.info(f"Loaded persisted throttle: enabled={self._throttle.enabled}")
        except Exception as e:
            logger.warning(f"Failed to load routing state: {e}")

    def _save_routing_state(self) -> None:
        """Persist routing + throttle config to disk."""
        if not self._persist:
            return
        try:
            data = {
                "routing": asdict(self._routing),
                "throttle": asdict(self._throttle),
            }
            with open(self.ROUTING_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save routing state: {e}")

    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        """Add a subscriber for real-time events."""
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        """Remove a subscriber."""
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        """Emit an event to all subscribers."""
        with self._lock:
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn({"event": event, "data": data})
            except Exception:
                pass

    def get_routing_config(self) -> RoutingConfig:
        """
        Return a copy of current routing configuration.

        Returns:
            RoutingConfig copy.
        """
        with self._lock:
            return RoutingConfig(**asdict(self._routing))

    def update_routing_config(self, updates: Dict[str, Any]) -> RoutingConfig:
        """
        Update routing configuration from dashboard API input.

        Args:
            updates: Partial routing configuration.

        Returns:
            Updated RoutingConfig copy.

        Raises:
            ValueError: If a field has an invalid value.
        """
        allowed = set(RoutingConfig.__dataclass_fields__.keys())
        unknown = sorted(set(updates.keys()) - allowed)
        if unknown:
            raise ValueError(f"Unknown routing field(s): {', '.join(unknown)}")

        with self._lock:
            data = asdict(self._routing)
            data.update(updates)

            if data["mode"] not in ROUTING_MODES:
                raise ValueError(
                    f"Invalid routing mode '{data['mode']}'. Expected one of: "
                    f"{', '.join(sorted(ROUTING_MODES))}"
                )
            if not isinstance(data["redirects"], dict):
                raise ValueError("redirects must be an object mapping source models to target models")
            if not isinstance(data["fallback_models"], list):
                raise ValueError("fallback_models must be a list of model names")
            if data["max_content_chars"] < 1000:
                raise ValueError("max_content_chars must be at least 1000")

            data["manual_model"] = str(data["manual_model"]).strip()
            data["redirects"] = {
                str(source).strip(): str(target).strip()
                for source, target in data["redirects"].items()
                if str(source).strip() and str(target).strip()
            }
            data["fallback_models"] = _dedupe_models(
                [str(model).strip() for model in data["fallback_models"]]
            )

            if data["mode"] == "manual" and data["enabled"] and not data["manual_model"]:
                raise ValueError("manual_model is required when manual routing is enabled")

            self._routing = RoutingConfig(**data)
            logger.info(
                f"Runtime routing updated: enabled={self._routing.enabled}, "
                f"mode={self._routing.mode}, manual_model={self._routing.manual_model}"
            )
            self._save_routing_state()
            return self.get_routing_config()

    def reset_routing_config(self) -> RoutingConfig:
        """
        Reset routing configuration to safe defaults.

        Returns:
            Reset RoutingConfig copy.
        """
        with self._lock:
            self._routing = RoutingConfig()
            logger.info("Runtime routing reset to defaults")
            self._save_routing_state()
            return self.get_routing_config()

    def get_throttle_config(self) -> ThrottleConfig:
        """Return a copy of current throttle configuration."""
        with self._lock:
            return ThrottleConfig(**asdict(self._throttle))

    def update_throttle_config(self, updates: Dict[str, Any]) -> ThrottleConfig:
        """
        Update throttle configuration from dashboard API input.

        Returns:
            Updated ThrottleConfig copy.

        Raises:
            ValueError: If a field has an invalid value.
        """
        allowed = set(ThrottleConfig.__dataclass_fields__.keys())
        unknown = sorted(set(updates.keys()) - allowed)
        if unknown:
            raise ValueError(f"Unknown throttle field(s): {', '.join(unknown)}")

        with self._lock:
            data = asdict(self._throttle)
            data.update(updates)

            if data["max_concurrent"] < 1:
                raise ValueError("max_concurrent must be >= 1")
            if data["jitter_ms"] < 0:
                raise ValueError("jitter_ms must be >= 0")

            self._throttle = ThrottleConfig(**data)
            logger.info(
                f"Throttle config updated: concurrent={self._throttle.max_concurrent}, "
                f"jitter={self._throttle.jitter_ms}ms, fast_fail={self._throttle.throttle_fast_fail}, "
                f"enabled={self._throttle.enabled}"
            )
            self._save_routing_state()
            return self.get_throttle_config()

    def route_model(self, requested_model: str) -> RoutingDecision:
        """
        Apply current runtime routing to a requested model.

        Args:
            requested_model: Model name from the client request.

        Returns:
            RoutingDecision describing the first attempt and fallback queue.
        """
        with self._lock:
            config = RoutingConfig(**asdict(self._routing))

        routed_model = requested_model
        applied = False
        reason = "Runtime routing disabled"

        if config.enabled and config.mode == "manual":
            routed_model = config.manual_model
            applied = routed_model != requested_model
            reason = f"Manual routing selected {routed_model}"
        elif config.enabled and config.mode == "redirect":
            normalized = normalize_model_name(requested_model)
            target = config.redirects.get(requested_model) or config.redirects.get(normalized)
            if target:
                routed_model = target
                applied = routed_model != requested_model
                reason = f"Redirect rule matched {requested_model} -> {routed_model}"
            else:
                reason = "No redirect rule matched"
        elif config.enabled:
            reason = "Passthrough mode"

        fallback_models: List[str] = []
        if config.enabled:
            if config.safe_fallback_to_original and routed_model != requested_model:
                fallback_models.append(requested_model)
            if config.fallback_enabled:
                fallback_models.extend(config.fallback_models)
        else:
            # Preserve the project's static fallback behavior while runtime
            # routing is disabled.
            fallback_models.extend(MODEL_FALLBACKS.get(requested_model, []))
        fallback_models = [
            model for model in _dedupe_models(fallback_models)
            if model != routed_model and not _is_quality_downgrade(routed_model, model)
        ]

        return RoutingDecision(
            original_model=requested_model,
            routed_model=routed_model,
            applied=applied,
            mode=config.mode if config.enabled else "disabled",
            reason=reason,
            fallback_models=fallback_models,
        )

    def should_retry_failure(self, status_code: int, fallback_models: List[str]) -> bool:
        """
        Decide whether a failed attempt should use a fallback model.

        Args:
            status_code: Upstream or proxy status code.
            fallback_models: Remaining fallback candidates.

        Returns:
            True if retrying with a fallback is allowed.
        """
        return bool(fallback_models) and status_code in MODEL_FAILURE_STATUS_CODES

    def start_request(
        self,
        api_format: str,
        path: str,
        stream: bool,
        decision: RoutingDecision,
    ) -> str:
        """
        Start monitoring a proxied request.

        Args:
            api_format: API surface name.
            path: Request path.
            stream: Whether the request is streaming.
            decision: Runtime routing decision.

        Returns:
            Request monitor ID.
        """
        request_id = uuid.uuid4().hex[:12]
        record = RequestRecord(
            id=request_id,
            api_format=api_format,
            path=path,
            stream=stream,
            original_model=decision.original_model,
            routed_model=decision.routed_model,
            active_model=decision.routed_model,
            routing_reason=decision.reason,
        )
        with self._lock:
            self._active_requests[request_id] = record
            if latency_enabled():
                self._tracers[request_id] = get_tracer(request_id)
        self._emit("request_started", asdict(record))
        return request_id

    def add_trace_stage(
        self,
        request_id: str,
        stage: str,
        duration_s: float,
        start_ts: Optional[float] = None,
    ) -> None:
        """
        Record a latency stage on the in-flight request's tracer.

        No-op when tracing is disabled or the request isn't tracked.
        """
        with self._lock:
            tracer = self._tracers.get(request_id)
        if tracer is not None:
            tracer.add(stage, duration_s, start_ts)

    def start_attempt(self, request_id: str, model: str, account_id: Optional[str]) -> None:
        """
        Record a new upstream attempt.

        Args:
            request_id: Monitor request ID.
            model: Model used for this attempt.
            account_id: Account used for this attempt, if available.
        """
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record:
                return
            record.active_model = model
            record.status = "connecting"
            record.updated_at = time.time()
            record.attempts.append(
                RequestAttempt(model=model, account_id=account_id, status="connecting")
            )
        self._emit("attempt", asdict(record))

    def update_request_status(self, request_id: str, status: str) -> None:
        """
        Update the status of an active request.

        Valid statuses: connecting, waiting_first_token, streaming, active
        """
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record:
                return
            record.status = status
            record.updated_at = time.time()
            if record.attempts:
                record.attempts[-1].status = status
        self._emit("attempt", asdict(record))

    def finish_attempt(
        self,
        request_id: str,
        http_status: Optional[int],
        error: Optional[str] = None,
    ) -> None:
        """
        Finish the latest upstream attempt.

        Args:
            request_id: Monitor request ID.
            http_status: Upstream or proxy status code.
            error: Error message, if any.
        """
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record or not record.attempts:
                return
            attempt = record.attempts[-1]
            attempt.finished_at = time.time()
            attempt.http_status = http_status
            attempt.error = error
            attempt.status = "ok" if http_status == 200 and not error else "failed"
            record.updated_at = attempt.finished_at
            self._emit("attempt", asdict(record))

    def record_payload(self, request_id: str, label: str, payload: Any) -> None:
        """
        Capture a bounded payload excerpt if content capture is enabled.

        Args:
            request_id: Monitor request ID.
            label: Payload label.
            payload: Payload content.
        """
        with self._lock:
            config = self._routing
            if not config.capture_content:
                return
            record = self._active_requests.get(request_id)
            if not record:
                return
            record.payloads[label] = _serialize_excerpt(payload, config.max_content_chars)
            record.updated_at = time.time()

    def record_chunk(self, request_id: str, chunk: Any) -> None:
        """
        Capture a streaming response chunk if content capture is enabled.

        Args:
            request_id: Monitor request ID.
            chunk: Stream chunk.
        """
        with self._lock:
            config = self._routing
            if not config.capture_content:
                return
            record = self._active_requests.get(request_id)
            if not record:
                return
            record.chunks.append(_serialize_excerpt(chunk, config.max_content_chars))
            if len(record.chunks) > self._max_chunks_per_request:
                record.chunks = record.chunks[-self._max_chunks_per_request:]
            record.updated_at = time.time()

    def record_metrics(
        self,
        request_id: str,
        ttft: Optional[float] = None,
        tps: Optional[float] = None,
        total_s: Optional[float] = None,
        output_tokens: Optional[int] = None,
        input_tokens: Optional[int] = None,
    ) -> None:
        """
        Record performance metrics on a request record.

        Args:
            request_id: Monitor request ID.
            ttft: Time to first token in seconds.
            tps: Tokens per second.
            total_s: Total stream duration in seconds.
            output_tokens: Number of output tokens.
            input_tokens: Number of input tokens.
        """
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

    def record_stream_progress(
        self,
        request_id: str,
        ttft: Optional[float] = None,
        tps: Optional[float] = None,
        output_tokens: Optional[int] = None,
        content_delta: Optional[str] = None,
    ) -> None:
        """
        Record live streaming progress and emit SSE event for dashboard.

        Called periodically during streaming to provide real-time visibility.
        """
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record:
                return
            if ttft is not None:
                record.ttft_s = round(ttft, 3)
            if tps is not None:
                record.tps = round(tps, 2)
            if output_tokens is not None:
                record.output_tokens = output_tokens
            if content_delta is not None:
                record.chunks.append(content_delta)
                if len(record.chunks) > self._max_chunks_per_request:
                    record.chunks = record.chunks[-self._max_chunks_per_request:]
            record.updated_at = time.time()

        # Emit live update event for SSE subscribers
        self._emit("stream_progress", {
            "id": request_id,
            "ttft_s": record.ttft_s,
            "tps": record.tps,
            "output_tokens": record.output_tokens,
            "content_delta": content_delta,
        })

    def record_trim(
        self,
        request_id: str,
        before: int,
        after: int,
        before_bytes: int,
        after_bytes: int,
    ) -> None:
        """
        Record payload trim statistics on a request record.

        Args:
            request_id: Monitor request ID.
            before: Number of messages before trimming.
            after: Number of messages after trimming.
            before_bytes: Payload size in bytes before trimming.
            after_bytes: Payload size in bytes after trimming.
        """
        with self._lock:
            record = self._active_requests.get(request_id)
            if not record:
                return
            record.trim_before_messages = before
            record.trim_after_messages = after
            record.trim_before_bytes = before_bytes
            record.trim_after_bytes = after_bytes
            record.updated_at = time.time()

    def finish_request(
        self,
        request_id: str,
        status: str,
        response: Any = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Mark a request complete and move it to completed history.

        Args:
            request_id: Monitor request ID.
            status: Final status.
            response: Optional response payload to capture.
            error: Optional error message.
        """
        with self._lock:
            record = self._active_requests.pop(request_id, None)
            tracer = self._tracers.pop(request_id, None)
            if not record:
                return

            now = time.time()
            record.status = status
            record.updated_at = now
            record.ended_at = now
            record.error = error

            if response is not None and self._routing.capture_content:
                record.response = _serialize_excerpt(response, self._routing.max_content_chars)

            # Finalize latency trace (if tracing was enabled when this request started)
            if tracer is not None:
                try:
                    if record.ttft_s is not None:
                        tracer.set_ttft(record.ttft_s)
                    tracer.set_total((record.ended_at or now) - record.started_at)
                    trace_obj = tracer.finalize()
                    record.trace = trace_obj.to_dict()
                    from kiro.metrics import stage_metrics_registry
                    stage_metrics_registry.record(now, trace_obj.per_stage_durations())
                except Exception:
                    pass

            try:
                from kiro.metrics import metrics_registry
                duration = (record.ended_at or now) - record.started_at
                is_error = status not in ("completed", "client_disconnected")
                metrics_registry.record(now, duration, is_error)
            except Exception:
                pass

            self._emit("request_finished", asdict(record))
            self._completed_requests.appendleft(record)

    def clear_monitor(self) -> None:
        """
        Clear completed request history.

        Active requests are intentionally preserved.
        """
        with self._lock:
            self._completed_requests.clear()

    def snapshot(self) -> Dict[str, Any]:
        """
        Return dashboard state.

        Returns:
            JSON-serializable dashboard snapshot.
        """
        with self._lock:
            throttle_data = asdict(self._throttle)
            # Merge live adaptive gate state
            try:
                from kiro.http_client import get_adaptive_gate
                throttle_data["gate"] = get_adaptive_gate().snapshot()
            except Exception:
                throttle_data["gate"] = None

            return {
                "routing": asdict(self._routing),
                "throttle": throttle_data,
                "active_requests": [asdict(record) for record in self._active_requests.values()],
                "completed_requests": [asdict(record) for record in list(self._completed_requests)],
                "server_time": time.time(),
            }


control_panel = ControlPanelState()
