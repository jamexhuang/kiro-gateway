# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
HTTP client for Kiro API with retry logic support.

Handles:
- 403: automatic token refresh and retry
- 429: exponential backoff
- 5xx: exponential backoff
- Timeouts: exponential backoff

Supports both per-request clients and shared application-level client
with connection pooling for better resource management.
"""

import asyncio
import random
import time
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger

from kiro.config import (
    MAX_RETRIES, BASE_RETRY_DELAY, FIRST_TOKEN_MAX_RETRIES, STREAMING_READ_TIMEOUT,
    MAX_CONCURRENT_UPSTREAM_REQUESTS, REQUEST_JITTER_MS, THROTTLE_FAST_FAIL,
)
from kiro.auth import KiroAuthManager
from kiro.utils import get_kiro_headers
from kiro.network_errors import classify_network_error, get_short_error_message, NetworkErrorInfo


# --- Adaptive burst protection (AIMD on gap between connection establishments) ---
class AdaptiveGate:
    """
    Single-slot gate that serializes connection establishment with adaptive gap.

    Streaming continues in parallel — gate only holds during "send → status code".
    On 429: gap doubles (multiplicative increase, capped at max_gap_ms).
    On consecutive successes: gap shrinks (additive decrease, floored at min_gap_ms).
    """

    def __init__(
        self,
        min_gap_ms: int = 0,
        max_gap_ms: int = 8000,
        initial_gap_ms: int = 200,
        success_threshold: int = 5,
        decrease_step_ms: int = 100,
    ):
        self._lock = asyncio.Lock()
        self._last_release_ts: float = 0.0
        self._current_gap_ms: int = initial_gap_ms
        self._min_gap_ms = min_gap_ms
        self._max_gap_ms = max_gap_ms
        self._success_threshold = success_threshold
        self._decrease_step_ms = decrease_step_ms
        self._consecutive_successes: int = 0
        self._consecutive_429s: int = 0

    async def acquire(self) -> float:
        """Acquire gate, enforcing min gap since last release. Returns waited seconds."""
        await self._lock.acquire()
        elapsed_ms = (time.monotonic() - self._last_release_ts) * 1000 if self._last_release_ts > 0 else self._current_gap_ms
        wait_ms = max(0, self._current_gap_ms - elapsed_ms)
        if wait_ms > 0:
            jitter_ms = random.uniform(0, min(wait_ms * 0.3, 200))
            total_wait_s = (wait_ms + jitter_ms) / 1000
            await asyncio.sleep(total_wait_s)
            return total_wait_s
        return 0.0

    def release(self) -> None:
        self._last_release_ts = time.monotonic()
        if self._lock.locked():
            self._lock.release()

    def report_429(self) -> None:
        """Multiplicative increase of gap on rate limit."""
        self._consecutive_successes = 0
        self._consecutive_429s += 1
        old = self._current_gap_ms
        self._current_gap_ms = min(self._max_gap_ms, max(self._current_gap_ms * 2, 500))
        logger.info(f"AdaptiveGate: 429 detected, gap {old}ms → {self._current_gap_ms}ms (consecutive_429s={self._consecutive_429s})")

    def report_success(self) -> None:
        """Additive decrease of gap after consecutive successes."""
        self._consecutive_429s = 0
        self._consecutive_successes += 1
        if self._consecutive_successes >= self._success_threshold and self._current_gap_ms > self._min_gap_ms:
            old = self._current_gap_ms
            self._current_gap_ms = max(self._min_gap_ms, self._current_gap_ms - self._decrease_step_ms)
            self._consecutive_successes = 0
            logger.debug(f"AdaptiveGate: {self._success_threshold} successes, gap {old}ms → {self._current_gap_ms}ms")

    def snapshot(self) -> dict:
        return {
            "current_gap_ms": self._current_gap_ms,
            "min_gap_ms": self._min_gap_ms,
            "max_gap_ms": self._max_gap_ms,
            "consecutive_successes": self._consecutive_successes,
            "consecutive_429s": self._consecutive_429s,
            "locked": self._lock.locked(),
        }

    def update_bounds(self, min_gap_ms: Optional[int] = None, max_gap_ms: Optional[int] = None) -> None:
        if min_gap_ms is not None:
            self._min_gap_ms = max(0, min_gap_ms)
        if max_gap_ms is not None:
            self._max_gap_ms = max(self._min_gap_ms, max_gap_ms)
        self._current_gap_ms = max(self._min_gap_ms, min(self._max_gap_ms, self._current_gap_ms))


_adaptive_gate: Optional[AdaptiveGate] = None
_burst_enabled: bool = True
_throttle_fast_fail: bool = THROTTLE_FAST_FAIL


def get_adaptive_gate() -> AdaptiveGate:
    global _adaptive_gate
    if _adaptive_gate is None:
        _adaptive_gate = AdaptiveGate(
            min_gap_ms=0,
            max_gap_ms=8000,
            initial_gap_ms=REQUEST_JITTER_MS,
        )
    return _adaptive_gate


def update_burst_settings(
    max_concurrent: Optional[int] = None,  # kept for API compat, ignored
    jitter_ms: Optional[int] = None,        # repurposed as initial gap
    throttle_fast_fail: Optional[bool] = None,
    enabled: Optional[bool] = None,
    min_gap_ms: Optional[int] = None,
    max_gap_ms: Optional[int] = None,
) -> None:
    """Called by dashboard route when user changes settings."""
    global _throttle_fast_fail, _burst_enabled
    gate = get_adaptive_gate()
    if jitter_ms is not None and min_gap_ms is None and max_gap_ms is None:
        # Treat jitter_ms as both min and current gap on first set
        gate.update_bounds(min_gap_ms=0, max_gap_ms=max(jitter_ms * 16, 8000))
    if min_gap_ms is not None or max_gap_ms is not None:
        gate.update_bounds(min_gap_ms=min_gap_ms, max_gap_ms=max_gap_ms)
    if throttle_fast_fail is not None:
        _throttle_fast_fail = throttle_fast_fail
    if enabled is not None:
        _burst_enabled = enabled


class KiroHttpClient:
    """
    HTTP client for Kiro API with retry logic support.
    
    Automatically handles errors and retries requests:
    - 403: refreshes token and retries
    - 429: waits with exponential backoff
    - 5xx: waits with exponential backoff
    - Timeouts: waits with exponential backoff
    
    Supports two modes of operation:
    1. Per-request client: Creates and owns its own httpx.AsyncClient
    2. Shared client: Uses an application-level shared client (recommended)
    
    Using a shared client reduces memory usage and enables connection pooling,
    which is especially important for handling concurrent requests.
    
    Attributes:
        auth_manager: Authentication manager for obtaining tokens
        client: httpx HTTP client (owned or shared)
    
    Example:
        >>> # Per-request client (legacy mode)
        >>> client = KiroHttpClient(auth_manager)
        >>> response = await client.request_with_retry(...)
        
        >>> # Shared client (recommended)
        >>> shared = httpx.AsyncClient(limits=httpx.Limits(...))
        >>> client = KiroHttpClient(auth_manager, shared_client=shared)
        >>> response = await client.request_with_retry(...)
    """
    
    def __init__(
        self,
        auth_manager: KiroAuthManager,
        shared_client: Optional[httpx.AsyncClient] = None
    ):
        """
        Initializes the HTTP client.
        
        Args:
            auth_manager: Authentication manager
            shared_client: Optional shared httpx.AsyncClient for connection pooling.
                          If provided, this client will be used instead of creating
                          a new one. The shared client will NOT be closed by close().
        """
        self.auth_manager = auth_manager
        self._shared_client = shared_client
        self._owns_client = shared_client is None
        self.client: Optional[httpx.AsyncClient] = shared_client
    
    async def _get_client(self, stream: bool = False) -> httpx.AsyncClient:
        """
        Returns or creates an HTTP client with proper timeouts.
        
        If a shared client was provided at initialization, it is returned as-is.
        Otherwise, creates a new client with appropriate timeout configuration.
        
        httpx timeouts:
        - connect: TCP handshake (DNS + TCP SYN/ACK)
        - read: waiting for data from server between chunks
        - write: sending data to server
        - pool: waiting for free connection from pool
        
        IMPORTANT: FIRST_TOKEN_TIMEOUT is NOT used here!
        It is applied in streaming_openai.py via asyncio.wait_for() to control
        the wait time for the first token from the model (retry business logic).
        
        Args:
            stream: If True, uses STREAMING_READ_TIMEOUT for read (only for new clients)
        
        Returns:
            Active HTTP client
        """
        # If using shared client, return it directly
        # Shared client should be pre-configured with appropriate timeouts
        if self._shared_client is not None:
            return self._shared_client
        
        # Create new client if needed (per-request mode)
        if self.client is None or self.client.is_closed:
            if stream:
                # For streaming:
                # - connect: 30 sec (TCP connection, usually < 1 sec)
                # - read: STREAMING_READ_TIMEOUT (300 sec) - model may "think" between chunks
                # - write/pool: standard values
                timeout_config = httpx.Timeout(
                    connect=30.0,
                    read=STREAMING_READ_TIMEOUT,
                    write=30.0,
                    pool=30.0
                )
                logger.debug(f"Creating streaming HTTP client (read_timeout={STREAMING_READ_TIMEOUT}s)")
            else:
                # For regular requests: single timeout of 300 sec
                timeout_config = httpx.Timeout(timeout=300.0)
                logger.debug("Creating non-streaming HTTP client (timeout=300s)")
            
            self.client = httpx.AsyncClient(timeout=timeout_config, follow_redirects=True)
        return self.client
    
    async def close(self) -> None:
        """
        Closes the HTTP client if this instance owns it.
        
        If using a shared client, this method does nothing - the shared client
        should be closed by the application lifecycle manager.
        
        Uses graceful exception handling to prevent errors during cleanup
        from masking the original exception in finally blocks.
        """
        # Don't close shared clients - they're managed by the application
        if not self._owns_client:
            return
        
        if self.client and not self.client.is_closed:
            try:
                await self.client.aclose()
            except Exception as e:
                # Log but don't propagate - we're in cleanup code
                # Propagating here could mask the original exception
                logger.warning(f"Error closing HTTP client: {e}")
    
    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
        stream: bool = False,
        max_retries_override: Optional[int] = None,
        *,
        monitor_request_id: Optional[str] = None,
    ) -> httpx.Response:
        """
        Executes an HTTP request with retry logic.
        
        Automatically handles various error types:
        - 403: refreshes token via auth_manager.force_refresh() and retries
        - 429: waits with exponential backoff (1s, 2s, 4s)
        - 5xx: waits with exponential backoff
        - Timeouts: waits with exponential backoff
        
        For streaming, STREAMING_READ_TIMEOUT is used for waiting between chunks.
        First token timeout is controlled separately in streaming_openai.py via asyncio.wait_for().
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            json_data: Optional JSON body (for POST/PUT/PATCH)
            params: Optional query parameters (for GET)
            stream: Use streaming (default False)
            max_retries_override: Optional request-specific retry attempt count.
        
        Returns:
            httpx.Response with successful response
        
        Raises:
            HTTPException: On failure after all attempts (502/504)
        """
        # Determine the number of retry attempts
        # FIRST_TOKEN_TIMEOUT is used in streaming_openai.py, not here
        if max_retries_override is not None:
            max_retries = max(1, max_retries_override)
        else:
            max_retries = FIRST_TOKEN_MAX_RETRIES if stream else MAX_RETRIES
        
        client = await self._get_client(stream=stream)
        last_error = None
        last_error_info: Optional[NetworkErrorInfo] = None
        last_response: Optional[httpx.Response] = None  # Для сохранения последнего 429/5xx
        
        for attempt in range(max_retries):
            try:
                # Get current token (auth_ms hook)
                _auth_t0 = time.monotonic()
                token = await self.auth_manager.get_access_token()
                _auth_dur = time.monotonic() - _auth_t0
                if monitor_request_id:
                    try:
                        from kiro.control_panel import control_panel as _cp
                        _cp.add_trace_stage(monitor_request_id, "auth_ms", _auth_dur, start_ts=_auth_t0)
                    except Exception:
                        pass
                headers = get_kiro_headers(self.auth_manager, token)

                # Build request kwargs based on parameters
                request_kwargs = {"headers": headers}

                if json_data is not None:
                    request_kwargs["json"] = json_data

                if params is not None:
                    request_kwargs["params"] = params

                # --- Adaptive gate: serialize connection establishment ---
                if _burst_enabled:
                    gate = get_adaptive_gate()
                    waited = await gate.acquire()
                    if waited > 0:
                        logger.debug(f"AdaptiveGate: waited {waited:.3f}s before sending (gap={gate._current_gap_ms}ms)")
                        if monitor_request_id:
                            try:
                                from kiro.control_panel import control_panel as _cp
                                _cp.add_trace_stage(monitor_request_id, "gate_wait_ms", waited)
                            except Exception:
                                pass
                    try:
                        _conn_t0 = time.monotonic()
                        if stream:
                            headers["Connection"] = "close"
                            req = client.build_request(method, url, **request_kwargs)
                            logger.debug("Sending request to Kiro API...")
                            response = await client.send(req, stream=True)
                        else:
                            logger.debug("Sending request to Kiro API...")
                            response = await client.request(method, url, **request_kwargs)
                        _conn_dur = time.monotonic() - _conn_t0
                        if monitor_request_id:
                            try:
                                from kiro.control_panel import control_panel as _cp
                                _cp.add_trace_stage(monitor_request_id, "upstream_connect_ms", _conn_dur, start_ts=_conn_t0)
                            except Exception:
                                pass
                    finally:
                        gate.release()
                else:
                    _conn_t0 = time.monotonic()
                    if stream:
                        headers["Connection"] = "close"
                        req = client.build_request(method, url, **request_kwargs)
                        logger.debug("Sending request to Kiro API...")
                        response = await client.send(req, stream=True)
                    else:
                        logger.debug("Sending request to Kiro API...")
                        response = await client.request(method, url, **request_kwargs)
                    _conn_dur = time.monotonic() - _conn_t0
                    if monitor_request_id:
                        try:
                            from kiro.control_panel import control_panel as _cp
                            _cp.add_trace_stage(monitor_request_id, "upstream_connect_ms", _conn_dur, start_ts=_conn_t0)
                        except Exception:
                            pass

                # Check status
                if response.status_code == 200:
                    if _burst_enabled:
                        get_adaptive_gate().report_success()
                    return response

                # 403 - token expired, refresh and retry
                if response.status_code == 403:
                    logger.warning(f"Received 403, refreshing token (attempt {attempt + 1}/{MAX_RETRIES})")
                    await self.auth_manager.force_refresh()
                    continue

                # 429 - rate limit, wait and retry
                if response.status_code == 429:
                    last_response = response
                    if _burst_enabled:
                        get_adaptive_gate().report_429()
                    fast_fail = False
                    if not stream:
                        try:
                            body_bytes = await response.aread()
                            import json as _json
                            payload = _json.loads(body_bytes.decode("utf-8", errors="replace"))
                            reason = payload.get("reason") or payload.get("error", {}).get("reason")
                            if reason == "INSUFFICIENT_MODEL_CAPACITY":
                                logger.warning(
                                    f"429 {reason}: skipping retry, letting caller failover"
                                )
                                fast_fail = True
                                response._content = body_bytes
                            elif reason == "THROTTLING" and _throttle_fast_fail:
                                logger.warning(
                                    f"429 {reason}: fast-fail enabled, letting caller failover"
                                )
                                fast_fail = True
                                response._content = body_bytes
                        except Exception:
                            pass
                    if fast_fail or attempt >= max_retries - 1:
                        break
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    jitter = delay * random.uniform(0, 0.5)
                    logger.warning(
                        f"Received 429, waiting {delay + jitter:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay + jitter)
                    continue

                # 5xx - server error, wait and retry
                if 500 <= response.status_code < 600:
                    last_response = response
                    if attempt >= max_retries - 1:
                        break
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    jitter = delay * random.uniform(0, 0.3)
                    logger.warning(f"Received {response.status_code}, waiting {delay + jitter:.1f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay + jitter)
                    continue

                # Other errors - return as is
                return response
                
            except httpx.TimeoutException as e:
                last_error = e

                # Classify timeout error for user-friendly messaging
                error_info = classify_network_error(e)
                last_error_info = error_info

                # Log with user-friendly message
                short_msg = get_short_error_message(error_info)

                if error_info.is_retryable and attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    jitter = delay * random.uniform(0, 0.3)
                    logger.warning(f"{short_msg} - waiting {delay + jitter:.1f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay + jitter)
                else:
                    logger.error(f"{short_msg} - no more retries (attempt {attempt + 1}/{max_retries})")
                    if not error_info.is_retryable:
                        break  # Don't retry non-retryable errors

            except httpx.RequestError as e:
                last_error = e

                # Classify the error for user-friendly messaging
                error_info = classify_network_error(e)
                last_error_info = error_info

                # Log with user-friendly message
                short_msg = get_short_error_message(error_info)

                if error_info.is_retryable and attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    jitter = delay * random.uniform(0, 0.3)
                    logger.warning(f"{short_msg} - waiting {delay + jitter:.1f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay + jitter)
                else:
                    logger.error(f"{short_msg} - no more retries (attempt {attempt + 1}/{max_retries})")
                    if not error_info.is_retryable:
                        break  # Don't retry non-retryable errors
        
        # If we have a last_response (429/5xx retry exhausted), return it
        # This allows the caller to see the real status code and error body
        if last_response is not None:
            logger.warning(
                f"Retries exhausted for HTTP {last_response.status_code}, "
                f"returning response to caller for classification"
            )
            return last_response
        
        # All attempts exhausted - provide detailed, user-friendly error message
        if last_error_info:
            # Use classified error information
            error_message = last_error_info.user_message
            
            # Add troubleshooting steps
            if last_error_info.troubleshooting_steps:
                error_message += "\n\nTroubleshooting:\n"
                for i, step in enumerate(last_error_info.troubleshooting_steps, 1):
                    error_message += f"{i}. {step}\n"
            
            # Add technical details for debugging
            error_message += f"\nTechnical details: {last_error_info.technical_details}"
            
            raise HTTPException(
                status_code=last_error_info.suggested_http_code,
                detail=error_message.strip()
            )
        else:
            # Fallback if no error was captured (shouldn't happen)
            if stream:
                raise HTTPException(
                    status_code=504,
                    detail=f"Streaming failed after {max_retries} attempts. Unknown error."
                )
            else:
                raise HTTPException(
                    status_code=502,
                    detail=f"Request failed after {max_retries} attempts. Unknown error."
                )
    
    async def __aenter__(self) -> "KiroHttpClient":
        """Async context manager support."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Closes the client when exiting context."""
        await self.close()
