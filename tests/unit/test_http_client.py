# -*- coding: utf-8 -*-

"""
Unit tests for KiroHttpClient.
Tests retry logic, error handling, and HTTP client management.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import HTTPException

from kiro.http_client import KiroHttpClient
from kiro.auth import KiroAuthManager
from kiro.config import MAX_RETRIES, BASE_RETRY_DELAY, FIRST_TOKEN_MAX_RETRIES, STREAMING_READ_TIMEOUT


@pytest.fixture
def mock_auth_manager_for_http():
    """Creates a mocked KiroAuthManager for HTTP client tests."""
    manager = Mock(spec=KiroAuthManager)
    manager.get_access_token = AsyncMock(return_value="test_access_token")
    manager.force_refresh = AsyncMock(return_value="new_access_token")
    manager.fingerprint = "test_fingerprint_12345678"
    manager._fingerprint = "test_fingerprint_12345678"
    return manager


class TestKiroHttpClientInitialization:
    """Tests for KiroHttpClient initialization."""
    
    def test_initialization_stores_auth_manager(self, mock_auth_manager_for_http):
        """
        What it does: Verifies auth_manager is stored during initialization.
        Purpose: Ensure auth_manager is available for obtaining tokens.
        """
        print("Setup: Creating KiroHttpClient...")
        client = KiroHttpClient(mock_auth_manager_for_http)
        
        print("Verification: auth_manager is stored...")
        assert client.auth_manager is mock_auth_manager_for_http
    
    def test_initialization_client_is_none(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that HTTP client is initially None.
        Purpose: Ensure lazy initialization.
        """
        print("Setup: Creating KiroHttpClient...")
        client = KiroHttpClient(mock_auth_manager_for_http)
        
        print("Verification: client is initially None...")
        assert client.client is None


class TestKiroHttpClientGetClient:
    """Tests for _get_client method."""
    
    @pytest.mark.asyncio
    async def test_get_client_creates_new_client(self, mock_auth_manager_for_http):
        """
        What it does: Verifies creation of a new HTTP client.
        Purpose: Ensure client is created on first call.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        print("Action: Getting client...")
        with patch('kiro.http_client.httpx.AsyncClient') as mock_async_client:
            mock_instance = AsyncMock()
            mock_instance.is_closed = False
            mock_async_client.return_value = mock_instance
            
            client = await http_client._get_client()
            
            print("Verification: Client created...")
            mock_async_client.assert_called_once()
            assert client is mock_instance
    
    @pytest.mark.asyncio
    async def test_get_client_reuses_existing_client(self, mock_auth_manager_for_http):
        """
        What it does: Verifies reuse of existing client.
        Purpose: Ensure client is not recreated unnecessarily.
        """
        print("Setup: Creating KiroHttpClient with existing client...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_existing = AsyncMock()
        mock_existing.is_closed = False
        http_client.client = mock_existing
        
        print("Action: Getting client...")
        client = await http_client._get_client()
        
        print("Verification: Existing client returned...")
        assert client is mock_existing
    
    @pytest.mark.asyncio
    async def test_get_client_recreates_closed_client(self, mock_auth_manager_for_http):
        """
        What it does: Verifies recreation of closed client.
        Purpose: Ensure closed client is replaced with a new one.
        """
        print("Setup: Creating KiroHttpClient with closed client...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_closed = AsyncMock()
        mock_closed.is_closed = True
        http_client.client = mock_closed
        
        print("Action: Getting client...")
        with patch('kiro.http_client.httpx.AsyncClient') as mock_async_client:
            mock_new = AsyncMock()
            mock_new.is_closed = False
            mock_async_client.return_value = mock_new
            
            client = await http_client._get_client()
            
            print("Verification: New client created...")
            mock_async_client.assert_called_once()
            assert client is mock_new


class TestKiroHttpClientClose:
    """Tests for close method."""
    
    @pytest.mark.asyncio
    async def test_close_closes_client(self, mock_auth_manager_for_http):
        """
        What it does: Verifies HTTP client closure.
        Purpose: Ensure aclose() is called.
        """
        print("Setup: Creating KiroHttpClient with client...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()
        http_client.client = mock_client
        
        print("Action: Closing client...")
        await http_client.close()
        
        print("Verification: aclose() called...")
        mock_client.aclose.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_close_does_nothing_for_none_client(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that close() doesn't fail for None client.
        Purpose: Ensure safe close() call without client.
        """
        print("Setup: Creating KiroHttpClient without client...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        print("Action: Closing client...")
        await http_client.close()  # Should not raise an error
        
        print("Verification: No errors...")
    
    @pytest.mark.asyncio
    async def test_close_does_nothing_for_closed_client(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that close() doesn't fail for closed client.
        Purpose: Ensure safe repeated close() call.
        """
        print("Setup: Creating KiroHttpClient with closed client...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_client = AsyncMock()
        mock_client.is_closed = True
        http_client.client = mock_client
        
        print("Action: Closing client...")
        await http_client.close()
        
        print("Verification: aclose() NOT called...")
        mock_client.aclose.assert_not_called()


class TestKiroHttpClientRequestWithRetry:
    """Tests for request_with_retry method."""
    
    @pytest.mark.asyncio
    async def test_successful_request_returns_response(self, mock_auth_manager_for_http):
        """
        What it does: Verifies successful request.
        Purpose: Ensure 200 response is returned immediately.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(return_value=mock_response)
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                response = await http_client.request_with_retry(
                    "POST",
                    "https://api.example.com/test",
                    {"data": "value"}
                )
        
        print("Verification: Response received...")
        assert response.status_code == 200
        mock_client.request.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_403_triggers_token_refresh(self, mock_auth_manager_for_http):
        """
        What it does: Verifies token refresh on 403.
        Purpose: Ensure force_refresh() is called on 403.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response_403 = AsyncMock()
        mock_response_403.status_code = 403
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=[mock_response_403, mock_response_200])
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                response = await http_client.request_with_retry(
                    "POST",
                    "https://api.example.com/test",
                    {"data": "value"}
                )
        
        print("Verification: force_refresh() called...")
        mock_auth_manager_for_http.force_refresh.assert_called_once()
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_429_triggers_backoff(self, mock_auth_manager_for_http):
        """
        What it does: Verifies exponential backoff on 429.
        Purpose: Ensure request is retried after delay.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response_429 = AsyncMock()
        mock_response_429.status_code = 429
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=[mock_response_429, mock_response_200])
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"}
                    )
        
        print("Verification: sleep() called for backoff...")
        mock_sleep.assert_called_once()
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_5xx_triggers_backoff(self, mock_auth_manager_for_http):
        """
        What it does: Verifies exponential backoff on 5xx.
        Purpose: Ensure server errors are handled with retry.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response_500 = AsyncMock()
        mock_response_500.status_code = 500
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=[mock_response_500, mock_response_200])
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"}
                    )
        
        print("Verification: sleep() called for backoff...")
        mock_sleep.assert_called_once()
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_timeout_triggers_backoff(self, mock_auth_manager_for_http):
        """
        What it does: Verifies exponential backoff on timeout.
        Purpose: Ensure timeouts are handled with retry.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=[
            httpx.TimeoutException("Timeout"),
            mock_response_200
        ])
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"}
                    )
        
        print("Verification: sleep() called for backoff...")
        mock_sleep.assert_called_once()
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_request_error_triggers_backoff(self, mock_auth_manager_for_http):
        """
        What it does: Verifies exponential backoff on request error.
        Purpose: Ensure network errors are handled with retry.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=[
            httpx.RequestError("Connection error"),
            mock_response_200
        ])
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"}
                    )
        
        print("Verification: sleep() called for backoff...")
        mock_sleep.assert_called_once()
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_max_retries_exceeded_raises_502(self, mock_auth_manager_for_http):
        """
        What it does: Verifies HTTPException is raised after exhausting retries.
        Purpose: Ensure 502 is raised after MAX_RETRIES.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', new_callable=AsyncMock):
                    with pytest.raises(HTTPException) as exc_info:
                        await http_client.request_with_retry(
                            "POST",
                            "https://api.example.com/test",
                            {"data": "value"}
                        )
        
        print(f"Verification: HTTPException with code 502...")
        assert exc_info.value.status_code == 502
        assert str(MAX_RETRIES) in exc_info.value.detail
    
    @pytest.mark.asyncio
    async def test_other_status_codes_returned_as_is(self, mock_auth_manager_for_http):
        """
        What it does: Verifies other status codes are returned without retry.
        Purpose: Ensure 400, 404, etc. are returned immediately.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 400
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(return_value=mock_response)
        
        print("Action: Executing request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                response = await http_client.request_with_retry(
                    "POST",
                    "https://api.example.com/test",
                    {"data": "value"}
                )
        
        print("Verification: 400 response returned without retry...")
        assert response.status_code == 400
        mock_client.request.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_streaming_request_uses_send(self, mock_auth_manager_for_http):
        """
        What it does: Verifies send() is used for streaming.
        Purpose: Ensure stream=True uses build_request + send.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        mock_client.send = AsyncMock(return_value=mock_response)
        
        print("Action: Executing streaming request...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                response = await http_client.request_with_retry(
                    "POST",
                    "https://api.example.com/test",
                    {"data": "value"},
                    stream=True
                )
        
        print("Verification: build_request and send called...")
        mock_client.build_request.assert_called_once()
        mock_client.send.assert_called_once_with(mock_request, stream=True)
        assert response.status_code == 200


class TestKiroHttpClientContextManager:
    """Tests for async context manager."""
    
    @pytest.mark.asyncio
    async def test_context_manager_returns_self(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that __aenter__ returns self.
        Purpose: Ensure correct async with behavior.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        print("Action: Entering context...")
        result = await http_client.__aenter__()
        
        print("Verification: self returned...")
        assert result is http_client
    
    @pytest.mark.asyncio
    async def test_context_manager_closes_on_exit(self, mock_auth_manager_for_http):
        """
        What it does: Verifies client closure on context exit.
        Purpose: Ensure close() is called in __aexit__.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()
        http_client.client = mock_client
        
        print("Action: Exiting context...")
        await http_client.__aexit__(None, None, None)
        
        print("Verification: aclose() called...")
        mock_client.aclose.assert_called_once()


class TestKiroHttpClientExponentialBackoff:
    """Tests for exponential backoff logic."""
    
    @pytest.mark.asyncio
    async def test_backoff_delay_increases_exponentially(self, mock_auth_manager_for_http):
        """
        What it does: Verifies exponential delay increase.
        Purpose: Ensure delay = BASE_RETRY_DELAY * (2 ** attempt).
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response_429 = AsyncMock()
        mock_response_429.status_code = 429
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        # 2 errors 429, then success (to verify 2 backoff delays)
        mock_client.request = AsyncMock(side_effect=[
            mock_response_429,
            mock_response_429,
            mock_response_200
        ])
        
        sleep_delays = []
        
        async def capture_sleep(delay):
            sleep_delays.append(delay)
        
        print("Action: Executing request with multiple retries...")
        with patch.object(http_client, '_get_client', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', side_effect=capture_sleep):
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"}
                    )
        
        print(f"Verification: Delays increase exponentially...")
        print(f"Delays: {sleep_delays}")
        assert len(sleep_delays) == 2
        assert sleep_delays[0] == BASE_RETRY_DELAY * (2 ** 0)  # 1.0
        assert sleep_delays[1] == BASE_RETRY_DELAY * (2 ** 1)  # 2.0


class TestKiroHttpClientStreamingTimeout:
    """Tests for streaming request timeout logic."""
    
    @pytest.mark.asyncio
    async def test_streaming_uses_streaming_read_timeout(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that streaming requests use STREAMING_READ_TIMEOUT.
        Purpose: Ensure stream=True uses httpx.Timeout with correct values.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        mock_client.send = AsyncMock(return_value=mock_response)
        
        print("Action: Executing streaming request...")
        with patch('kiro.http_client.httpx.AsyncClient') as mock_async_client:
            mock_async_client.return_value = mock_client
            
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                response = await http_client.request_with_retry(
                    "POST",
                    "https://api.example.com/test",
                    {"data": "value"},
                    stream=True
                )
        
        print("Verification: AsyncClient created with httpx.Timeout for streaming...")
        call_args = mock_async_client.call_args
        timeout_arg = call_args.kwargs.get('timeout')
        assert timeout_arg is not None, f"timeout not found in call_args: {call_args}"
        print(f"Comparing connect: Expected 30.0, Got {timeout_arg.connect}")
        assert timeout_arg.connect == 30.0, f"Expected connect=30.0, got {timeout_arg.connect}"
        print(f"Comparing read: Expected {STREAMING_READ_TIMEOUT}, Got {timeout_arg.read}")
        assert timeout_arg.read == STREAMING_READ_TIMEOUT, f"Expected read={STREAMING_READ_TIMEOUT}, got {timeout_arg.read}"
        assert call_args.kwargs.get('follow_redirects') == True
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_streaming_uses_first_token_max_retries(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that streaming requests use FIRST_TOKEN_MAX_RETRIES.
        Purpose: Ensure stream=True uses separate retry counter.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        mock_client.send = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
        
        print("Action: Executing streaming request with timeouts...")
        with patch('kiro.http_client.httpx.AsyncClient', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with pytest.raises(HTTPException) as exc_info:
                    await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"},
                        stream=True
                    )
        
        print(f"Verification: HTTPException with code 504...")
        assert exc_info.value.status_code == 504
        assert str(FIRST_TOKEN_MAX_RETRIES) in exc_info.value.detail
        
        print(f"Verification: Attempt count = FIRST_TOKEN_MAX_RETRIES ({FIRST_TOKEN_MAX_RETRIES})...")
        assert mock_client.send.call_count == FIRST_TOKEN_MAX_RETRIES
    
    @pytest.mark.asyncio
    async def test_streaming_timeout_retry_without_delay(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that streaming timeout retry happens without delay.
        Purpose: Ensure no exponential backoff on first token timeout.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        # First timeout, then success
        mock_client.send = AsyncMock(side_effect=[
            httpx.TimeoutException("Timeout"),
            mock_response
        ])
        
        sleep_called = False
        
        async def capture_sleep(delay):
            nonlocal sleep_called
            sleep_called = True
        
        print("Action: Executing streaming request with one timeout...")
        with patch('kiro.http_client.httpx.AsyncClient', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', side_effect=capture_sleep):
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"},
                        stream=True
                    )
        
        print("Verification: sleep() NOT called for streaming timeout...")
        assert not sleep_called
        assert response.status_code == 200
        
    @pytest.mark.asyncio
    async def test_non_streaming_uses_default_timeout(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that non-streaming requests use 300 seconds.
        Purpose: Ensure stream=False uses unified httpx.Timeout.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(return_value=mock_response)
        
        print("Action: Executing non-streaming request...")
        with patch('kiro.http_client.httpx.AsyncClient') as mock_async_client:
            mock_async_client.return_value = mock_client
            
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                response = await http_client.request_with_retry(
                    "POST",
                    "https://api.example.com/test",
                    {"data": "value"},
                    stream=False
                )
        
        print("Verification: AsyncClient created with httpx.Timeout(timeout=300)...")
        call_args = mock_async_client.call_args
        timeout_arg = call_args.kwargs.get('timeout')
        assert timeout_arg is not None, f"timeout not found in call_args: {call_args}"
        # httpx.Timeout(timeout=300) sets all timeouts to 300
        print(f"Comparing timeout: Expected 300.0 for all, Got connect={timeout_arg.connect}")
        assert timeout_arg.connect == 300.0
        assert timeout_arg.read == 300.0
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_connect_timeout_logged_correctly(self, mock_auth_manager_for_http):
        """
        What it does: Verifies ConnectTimeout logging.
        Purpose: Ensure ConnectTimeout is logged with correct type.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        # First ConnectTimeout, then success
        mock_client.send = AsyncMock(side_effect=[
            httpx.ConnectTimeout("Connection timeout"),
            mock_response
        ])
        
        print("Action: Executing streaming request with ConnectTimeout...")
        with patch('kiro.http_client.httpx.AsyncClient', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.logger') as mock_logger:
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"},
                        stream=True
                    )
        
        print("Verification: logger.warning called with [ConnectTimeout]...")
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any("ConnectTimeout" in call for call in warning_calls), f"ConnectTimeout not found in: {warning_calls}"
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_read_timeout_logged_correctly(self, mock_auth_manager_for_http):
        """
        What it does: Verifies ReadTimeout logging.
        Purpose: Ensure ReadTimeout is logged with STREAMING_READ_TIMEOUT.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        # First ReadTimeout, then success
        mock_client.send = AsyncMock(side_effect=[
            httpx.ReadTimeout("Read timeout"),
            mock_response
        ])
        
        print("Action: Executing streaming request with ReadTimeout...")
        with patch('kiro.http_client.httpx.AsyncClient', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.logger') as mock_logger:
                    response = await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"},
                        stream=True
                    )
        
        print("Verification: logger.warning called with [ReadTimeout] and STREAMING_READ_TIMEOUT...")
        warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
        assert any("ReadTimeout" in call for call in warning_calls), f"ReadTimeout not found in: {warning_calls}"
        assert any(str(STREAMING_READ_TIMEOUT) in call for call in warning_calls), f"STREAMING_READ_TIMEOUT not found in: {warning_calls}"
        assert response.status_code == 200
    
    @pytest.mark.asyncio
    async def test_streaming_timeout_returns_504_with_error_type(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that streaming timeout returns 504 with error type.
        Purpose: Ensure 504 is returned with error info after exhausting retries.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_request = Mock()
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.build_request = Mock(return_value=mock_request)
        mock_client.send = AsyncMock(side_effect=httpx.ReadTimeout("Timeout"))
        
        print("Action: Executing streaming request with persistent timeouts...")
        with patch('kiro.http_client.httpx.AsyncClient', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with pytest.raises(HTTPException) as exc_info:
                    await http_client.request_with_retry(
                        "POST",
                        "https://api.example.com/test",
                        {"data": "value"},
                        stream=True
                    )
        
        print("Verification: HTTPException with code 504 and error type...")
        print(f"Comparing status_code: Expected 504, Got {exc_info.value.status_code}")
        assert exc_info.value.status_code == 504
        print(f"Comparing detail: Expected 'ReadTimeout' in '{exc_info.value.detail}'")
        assert "ReadTimeout" in exc_info.value.detail
        assert "Streaming failed" in exc_info.value.detail
    
    @pytest.mark.asyncio
    async def test_non_streaming_timeout_returns_502(self, mock_auth_manager_for_http):
        """
        What it does: Verifies that non-streaming timeout returns 502.
        Purpose: Ensure non-streaming uses legacy logic with 502.
        """
        print("Setup: Creating KiroHttpClient...")
        http_client = KiroHttpClient(mock_auth_manager_for_http)
        
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.request = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
        
        print("Action: Executing non-streaming request with persistent timeouts...")
        with patch('kiro.http_client.httpx.AsyncClient', return_value=mock_client):
            with patch('kiro.http_client.get_kiro_headers', return_value={}):
                with patch('kiro.http_client.asyncio.sleep', new_callable=AsyncMock):
                    with pytest.raises(HTTPException) as exc_info:
                        await http_client.request_with_retry(
                            "POST",
                            "https://api.example.com/test",
                            {"data": "value"},
                            stream=False
                        )
        
        print("Verification: HTTPException with code 502...")
        assert exc_info.value.status_code == 502