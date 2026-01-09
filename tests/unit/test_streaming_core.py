
# -*- coding: utf-8 -*-

"""
Unit tests for streaming_core module.

Tests for:
- KiroEvent dataclass
- StreamResult dataclass
- FirstTokenTimeoutError exception
- parse_kiro_stream() function
- collect_stream_to_result() function
- calculate_tokens_from_context_usage() function
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import asdict

from kiro.streaming_core import (
    KiroEvent,
    StreamResult,
    FirstTokenTimeoutError,
    parse_kiro_stream,
    collect_stream_to_result,
    calculate_tokens_from_context_usage,
    _process_chunk,
)


# ==================================================================================================
# Fixtures
# ==================================================================================================

@pytest.fixture
def mock_model_cache():
    """Mock for ModelInfoCache."""
    cache = MagicMock()
    cache.get_max_input_tokens.return_value = 200000
    return cache


@pytest.fixture
def mock_response():
    """Mock for httpx.Response."""
    response = AsyncMock()
    response.status_code = 200
    response.aclose = AsyncMock()
    return response


@pytest.fixture
def mock_parser():
    """Mock for AwsEventStreamParser."""
    parser = MagicMock()
    parser.feed.return_value = []
    parser.get_tool_calls.return_value = []
    return parser


# ==================================================================================================
# Tests for KiroEvent dataclass
# ==================================================================================================

class TestKiroEvent:
    """Tests for KiroEvent dataclass."""
    
    def test_creates_content_event(self):
        """
        What it does: Creates a content event with text.
        Goal: Verify KiroEvent can represent content events.
        """
        print("Action: Creating content event...")
        event = KiroEvent(type="content", content="Hello, world!")
        
        print(f"Comparing type: Expected 'content', Got '{event.type}'")
        assert event.type == "content"
        print(f"Comparing content: Expected 'Hello, world!', Got '{event.content}'")
        assert event.content == "Hello, world!"
        assert event.thinking_content is None
        assert event.tool_use is None
        print("✓ Content event created correctly")
    
    def test_creates_thinking_event(self):
        """
        What it does: Creates a thinking event with reasoning content.
        Goal: Verify KiroEvent can represent thinking events.
        """
        print("Action: Creating thinking event...")
        event = KiroEvent(
            type="thinking",
            thinking_content="Let me think...",
            is_first_thinking_chunk=True,
            is_last_thinking_chunk=False
        )
        
        print(f"Comparing type: Expected 'thinking', Got '{event.type}'")
        assert event.type == "thinking"
        print(f"Comparing thinking_content: Expected 'Let me think...', Got '{event.thinking_content}'")
        assert event.thinking_content == "Let me think..."
        assert event.is_first_thinking_chunk is True
        assert event.is_last_thinking_chunk is False
        print("✓ Thinking event created correctly")
    
    def test_creates_tool_use_event(self):
        """
        What it does: Creates a tool_use event with tool data.
        Goal: Verify KiroEvent can represent tool use events.
        """
        print("Action: Creating tool_use event...")
        tool_data = {
            "id": "call_123",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Moscow"}'}
        }
        event = KiroEvent(type="tool_use", tool_use=tool_data)
        
        print(f"Comparing type: Expected 'tool_use', Got '{event.type}'")
        assert event.type == "tool_use"
        print(f"Comparing tool_use: Expected {tool_data}, Got {event.tool_use}")
        assert event.tool_use == tool_data
        print("✓ Tool use event created correctly")
    
    def test_creates_usage_event(self):
        """
        What it does: Creates a usage event with metering data.
        Goal: Verify KiroEvent can represent usage events.
        """
        print("Action: Creating usage event...")
        usage_data = {"credits": 0.001}
        event = KiroEvent(type="usage", usage=usage_data)
        
        print(f"Comparing type: Expected 'usage', Got '{event.type}'")
        assert event.type == "usage"
        print(f"Comparing usage: Expected {usage_data}, Got {event.usage}")
        assert event.usage == usage_data
        print("✓ Usage event created correctly")
    
    def test_creates_context_usage_event(self):
        """
        What it does: Creates a context_usage event with percentage.
        Goal: Verify KiroEvent can represent context usage events.
        """
        print("Action: Creating context_usage event...")
        event = KiroEvent(type="context_usage", context_usage_percentage=5.5)
        
        print(f"Comparing type: Expected 'context_usage', Got '{event.type}'")
        assert event.type == "context_usage"
        print(f"Comparing context_usage_percentage: Expected 5.5, Got {event.context_usage_percentage}")
        assert event.context_usage_percentage == 5.5
        print("✓ Context usage event created correctly")
    
    def test_default_values(self):
        """
        What it does: Verifies default values for optional fields.
        Goal: Ensure all optional fields default to None/False.
        """
        print("Action: Creating minimal event...")
        event = KiroEvent(type="content")
        
        print("Checking default values...")
        assert event.content is None
        assert event.thinking_content is None
        assert event.tool_use is None
        assert event.usage is None
        assert event.context_usage_percentage is None
        assert event.is_first_thinking_chunk is False
        assert event.is_last_thinking_chunk is False
        print("✓ All default values are correct")


# ==================================================================================================
# Tests for StreamResult dataclass
# ==================================================================================================

class TestStreamResult:
    """Tests for StreamResult dataclass."""
    
    def test_creates_empty_result(self):
        """
        What it does: Creates an empty StreamResult.
        Goal: Verify default values are correct.
        """
        print("Action: Creating empty StreamResult...")
        result = StreamResult()
        
        print("Checking default values...")
        assert result.content == ""
        assert result.thinking_content == ""
        assert result.tool_calls == []
        assert result.usage is None
        assert result.context_usage_percentage is None
        print("✓ Empty StreamResult created correctly")
    
    def test_creates_result_with_content(self):
        """
        What it does: Creates StreamResult with content.
        Goal: Verify content is stored correctly.
        """
        print("Action: Creating StreamResult with content...")
        result = StreamResult(content="Hello, world!")
        
        print(f"Comparing content: Expected 'Hello, world!', Got '{result.content}'")
        assert result.content == "Hello, world!"
        print("✓ StreamResult with content created correctly")
    
    def test_creates_result_with_tool_calls(self):
        """
        What it does: Creates StreamResult with tool calls.
        Goal: Verify tool calls are stored correctly.
        """
        print("Action: Creating StreamResult with tool calls...")
        tool_calls = [
            {"id": "call_1", "function": {"name": "func1"}},
            {"id": "call_2", "function": {"name": "func2"}}
        ]
        result = StreamResult(tool_calls=tool_calls)
        
        print(f"Comparing tool_calls count: Expected 2, Got {len(result.tool_calls)}")
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0]["id"] == "call_1"
        print("✓ StreamResult with tool calls created correctly")
    
    def test_creates_result_with_usage(self):
        """
        What it does: Creates StreamResult with usage data.
        Goal: Verify usage is stored correctly.
        """
        print("Action: Creating StreamResult with usage...")
        usage = {"credits": 0.002}
        result = StreamResult(usage=usage)
        
        print(f"Comparing usage: Expected {usage}, Got {result.usage}")
        assert result.usage == usage
        print("✓ StreamResult with usage created correctly")
    
    def test_creates_full_result(self):
        """
        What it does: Creates StreamResult with all fields.
        Goal: Verify all fields work together.
        """
        print("Action: Creating full StreamResult...")
        result = StreamResult(
            content="Response text",
            thinking_content="Thinking...",
            tool_calls=[{"id": "call_1"}],
            usage={"credits": 0.001},
            context_usage_percentage=3.5
        )
        
        print("Checking all fields...")
        assert result.content == "Response text"
        assert result.thinking_content == "Thinking..."
        assert len(result.tool_calls) == 1
        assert result.usage == {"credits": 0.001}
        assert result.context_usage_percentage == 3.5
        print("✓ Full StreamResult created correctly")


# ==================================================================================================
# Tests for FirstTokenTimeoutError
# ==================================================================================================

class TestFirstTokenTimeoutError:
    """Tests for FirstTokenTimeoutError exception."""
    
    def test_creates_exception_with_message(self):
        """
        What it does: Creates exception with custom message.
        Goal: Verify exception message is stored correctly.
        """
        print("Action: Creating FirstTokenTimeoutError...")
        error = FirstTokenTimeoutError("No response within 30 seconds")
        
        print(f"Comparing message: Expected 'No response within 30 seconds', Got '{str(error)}'")
        assert str(error) == "No response within 30 seconds"
        print("✓ Exception created correctly")
    
    def test_exception_is_catchable(self):
        """
        What it does: Verifies exception can be caught.
        Goal: Ensure exception inherits from Exception.
        """
        print("Action: Raising and catching FirstTokenTimeoutError...")
        
        with pytest.raises(FirstTokenTimeoutError) as exc_info:
            raise FirstTokenTimeoutError("Timeout!")
        
        print(f"Caught exception: {exc_info.value}")
        assert "Timeout!" in str(exc_info.value)
        print("✓ Exception is catchable")
    
    def test_exception_inherits_from_exception(self):
        """
        What it does: Verifies inheritance chain.
        Goal: Ensure proper exception hierarchy.
        """
        print("Action: Checking inheritance...")
        error = FirstTokenTimeoutError("Test")
        
        assert isinstance(error, Exception)
        print("✓ FirstTokenTimeoutError inherits from Exception")


# ==================================================================================================
# Tests for parse_kiro_stream()
# ==================================================================================================

class TestParseKiroStream:
    """Tests for parse_kiro_stream() function."""
    
    @pytest.mark.asyncio
    async def test_parses_content_events(self, mock_response, mock_parser):
        """
        What it does: Parses content events from Kiro stream.
        Goal: Verify content events are yielded correctly.
        """
        print("Setup: Mock parser to return content events...")
        mock_parser.feed.return_value = [
            {"type": "content", "data": "Hello"},
            {"type": "content", "data": " World"}
        ]
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                    events.append(event)
        
        print(f"Received {len(events)} events")
        content_events = [e for e in events if e.type == "content"]
        print(f"Content events: {len(content_events)}")
        
        assert len(content_events) == 2
        assert content_events[0].content == "Hello"
        assert content_events[1].content == " World"
        print("✓ Content events parsed correctly")
    
    @pytest.mark.asyncio
    async def test_parses_usage_events(self, mock_response, mock_parser):
        """
        What it does: Parses usage events from Kiro stream.
        Goal: Verify usage events are yielded correctly.
        """
        print("Setup: Mock parser to return usage event...")
        mock_parser.feed.return_value = [
            {"type": "usage", "data": {"credits": 0.001}}
        ]
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                    events.append(event)
        
        print(f"Received {len(events)} events")
        usage_events = [e for e in events if e.type == "usage"]
        
        assert len(usage_events) == 1
        assert usage_events[0].usage == {"credits": 0.001}
        print("✓ Usage events parsed correctly")
    
    @pytest.mark.asyncio
    async def test_parses_context_usage_events(self, mock_response, mock_parser):
        """
        What it does: Parses context_usage events from Kiro stream.
        Goal: Verify context usage percentage is yielded correctly.
        """
        print("Setup: Mock parser to return context_usage event...")
        mock_parser.feed.return_value = [
            {"type": "context_usage", "data": 5.5}
        ]
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                    events.append(event)
        
        print(f"Received {len(events)} events")
        context_events = [e for e in events if e.type == "context_usage"]
        
        assert len(context_events) == 1
        assert context_events[0].context_usage_percentage == 5.5
        print("✓ Context usage events parsed correctly")
    
    @pytest.mark.asyncio
    async def test_yields_tool_calls_at_end(self, mock_response, mock_parser):
        """
        What it does: Yields tool calls collected during parsing.
        Goal: Verify tool calls are yielded as events.
        """
        print("Setup: Mock parser with tool calls...")
        mock_parser.feed.return_value = [{"type": "content", "data": "text"}]
        mock_parser.get_tool_calls.return_value = [
            {"id": "call_1", "function": {"name": "func1", "arguments": "{}"}}
        ]
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                    events.append(event)
        
        print(f"Received {len(events)} events")
        tool_events = [e for e in events if e.type == "tool_use"]
        
        assert len(tool_events) == 1
        assert tool_events[0].tool_use["id"] == "call_1"
        print("✓ Tool calls yielded correctly")
    
    @pytest.mark.asyncio
    async def test_raises_timeout_on_first_token(self, mock_response):
        """
        What it does: Raises FirstTokenTimeoutError on timeout.
        Goal: Verify timeout handling for first token.
        """
        print("Setup: Mock response that times out...")
        
        async def mock_aiter_bytes():
            yield b'chunk'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        async def mock_wait_for_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()
        
        print("Action: Parsing stream with timeout...")
        
        with patch('kiro.streaming_core.asyncio.wait_for', side_effect=mock_wait_for_timeout):
            with pytest.raises(FirstTokenTimeoutError) as exc_info:
                async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                    pass
        
        print(f"Caught exception: {exc_info.value}")
        assert "30" in str(exc_info.value)
        print("✓ FirstTokenTimeoutError raised on timeout")
    
    @pytest.mark.asyncio
    async def test_handles_empty_response(self, mock_response):
        """
        What it does: Handles empty response gracefully.
        Goal: Verify no events yielded for empty response.
        """
        print("Setup: Mock empty response...")
        
        async def mock_aiter_bytes():
            return
            yield  # Make it a generator
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        # Mock wait_for to raise StopAsyncIteration (empty response)
        async def mock_wait_for_empty(*args, **kwargs):
            raise StopAsyncIteration()
        
        print("Action: Parsing empty stream...")
        events = []
        
        with patch('kiro.streaming_core.asyncio.wait_for', side_effect=mock_wait_for_empty):
            async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                events.append(event)
        
        print(f"Received {len(events)} events")
        assert len(events) == 0
        print("✓ Empty response handled correctly")
    
    @pytest.mark.asyncio
    async def test_handles_generator_exit(self, mock_response, mock_parser):
        """
        What it does: Handles GeneratorExit gracefully.
        Goal: Verify client disconnect is handled.
        """
        print("Setup: Mock response that raises GeneratorExit...")
        
        async def mock_aiter_bytes():
            yield b'chunk1'
            raise GeneratorExit()
        
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        
        print("Action: Parsing stream with GeneratorExit...")
        events = []
        generator_exit_raised = False
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                try:
                    async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                        events.append(event)
                except GeneratorExit:
                    generator_exit_raised = True
        
        print(f"GeneratorExit raised: {generator_exit_raised}")
        assert generator_exit_raised
        print("✓ GeneratorExit handled correctly")


# ==================================================================================================
# Tests for _process_chunk()
# ==================================================================================================

class TestProcessChunk:
    """Tests for _process_chunk() helper function."""
    
    @pytest.mark.asyncio
    async def test_processes_content_event(self, mock_parser):
        """
        What it does: Processes content event from chunk.
        Goal: Verify content is converted to KiroEvent.
        """
        print("Setup: Mock parser with content event...")
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        
        print("Action: Processing chunk...")
        events = []
        async for event in _process_chunk(mock_parser, b'chunk', None):
            events.append(event)
        
        print(f"Received {len(events)} events")
        assert len(events) == 1
        assert events[0].type == "content"
        assert events[0].content == "Hello"
        print("✓ Content event processed correctly")
    
    @pytest.mark.asyncio
    async def test_processes_usage_event(self, mock_parser):
        """
        What it does: Processes usage event from chunk.
        Goal: Verify usage is converted to KiroEvent.
        """
        print("Setup: Mock parser with usage event...")
        mock_parser.feed.return_value = [{"type": "usage", "data": {"credits": 0.001}}]
        
        print("Action: Processing chunk...")
        events = []
        async for event in _process_chunk(mock_parser, b'chunk', None):
            events.append(event)
        
        print(f"Received {len(events)} events")
        assert len(events) == 1
        assert events[0].type == "usage"
        assert events[0].usage == {"credits": 0.001}
        print("✓ Usage event processed correctly")
    
    @pytest.mark.asyncio
    async def test_processes_context_usage_event(self, mock_parser):
        """
        What it does: Processes context_usage event from chunk.
        Goal: Verify context usage is converted to KiroEvent.
        """
        print("Setup: Mock parser with context_usage event...")
        mock_parser.feed.return_value = [{"type": "context_usage", "data": 7.5}]
        
        print("Action: Processing chunk...")
        events = []
        async for event in _process_chunk(mock_parser, b'chunk', None):
            events.append(event)
        
        print(f"Received {len(events)} events")
        assert len(events) == 1
        assert events[0].type == "context_usage"
        assert events[0].context_usage_percentage == 7.5
        print("✓ Context usage event processed correctly")
    
    @pytest.mark.asyncio
    async def test_processes_multiple_events(self, mock_parser):
        """
        What it does: Processes multiple events from single chunk.
        Goal: Verify all events are yielded.
        """
        print("Setup: Mock parser with multiple events...")
        mock_parser.feed.return_value = [
            {"type": "content", "data": "Hello"},
            {"type": "content", "data": " World"},
            {"type": "usage", "data": {"credits": 0.001}}
        ]
        
        print("Action: Processing chunk...")
        events = []
        async for event in _process_chunk(mock_parser, b'chunk', None):
            events.append(event)
        
        print(f"Received {len(events)} events")
        assert len(events) == 3
        assert events[0].type == "content"
        assert events[1].type == "content"
        assert events[2].type == "usage"
        print("✓ Multiple events processed correctly")
    
    @pytest.mark.asyncio
    async def test_processes_with_thinking_parser(self, mock_parser):
        """
        What it does: Processes content through thinking parser.
        Goal: Verify thinking parser integration.
        """
        print("Setup: Mock parser and thinking parser...")
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        
        mock_thinking_parser = MagicMock()
        mock_thinking_parser.feed.return_value = MagicMock(
            thinking_content=None,
            regular_content="Hello",
            is_first_thinking_chunk=False,
            is_last_thinking_chunk=False
        )
        
        print("Action: Processing chunk with thinking parser...")
        events = []
        async for event in _process_chunk(mock_parser, b'chunk', mock_thinking_parser):
            events.append(event)
        
        print(f"Received {len(events)} events")
        assert len(events) == 1
        assert events[0].type == "content"
        assert events[0].content == "Hello"
        print("✓ Thinking parser integration works correctly")
    
    @pytest.mark.asyncio
    async def test_yields_thinking_content(self, mock_parser):
        """
        What it does: Yields thinking content from thinking parser.
        Goal: Verify thinking events are created.
        """
        print("Setup: Mock parser and thinking parser with thinking content...")
        mock_parser.feed.return_value = [{"type": "content", "data": "<thinking>Let me think</thinking>"}]
        
        mock_thinking_parser = MagicMock()
        mock_thinking_parser.feed.return_value = MagicMock(
            thinking_content="Let me think",
            regular_content=None,
            is_first_thinking_chunk=True,
            is_last_thinking_chunk=True
        )
        mock_thinking_parser.process_for_output.return_value = "Let me think"
        
        print("Action: Processing chunk with thinking content...")
        events = []
        async for event in _process_chunk(mock_parser, b'chunk', mock_thinking_parser):
            events.append(event)
        
        print(f"Received {len(events)} events")
        thinking_events = [e for e in events if e.type == "thinking"]
        assert len(thinking_events) == 1
        assert thinking_events[0].thinking_content == "Let me think"
        print("✓ Thinking content yielded correctly")


# ==================================================================================================
# Tests for collect_stream_to_result()
# ==================================================================================================

class TestCollectStreamToResult:
    """Tests for collect_stream_to_result() function."""
    
    @pytest.mark.asyncio
    async def test_collects_content(self, mock_response, mock_parser):
        """
        What it does: Collects content from stream.
        Goal: Verify content is accumulated correctly.
        """
        print("Setup: Mock parser with content events...")
        mock_parser.feed.return_value = [
            {"type": "content", "data": "Hello"},
            {"type": "content", "data": " World"}
        ]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Collecting stream...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with patch('kiro.streaming_core.parse_bracket_tool_calls', return_value=[]):
                    result = await collect_stream_to_result(mock_response, first_token_timeout=30)
        
        print(f"Collected content: '{result.content}'")
        assert result.content == "Hello World"
        print("✓ Content collected correctly")
    
    @pytest.mark.asyncio
    async def test_collects_tool_calls(self, mock_response, mock_parser):
        """
        What it does: Collects tool calls from stream.
        Goal: Verify tool calls are accumulated correctly.
        """
        print("Setup: Mock parser with tool calls...")
        mock_parser.feed.return_value = [{"type": "content", "data": "text"}]
        mock_parser.get_tool_calls.return_value = [
            {"id": "call_1", "function": {"name": "func1", "arguments": "{}"}}
        ]
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Collecting stream...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with patch('kiro.streaming_core.parse_bracket_tool_calls', return_value=[]):
                    result = await collect_stream_to_result(mock_response, first_token_timeout=30)
        
        print(f"Collected tool calls: {len(result.tool_calls)}")
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["id"] == "call_1"
        print("✓ Tool calls collected correctly")
    
    @pytest.mark.asyncio
    async def test_collects_usage(self, mock_response, mock_parser):
        """
        What it does: Collects usage from stream.
        Goal: Verify usage is stored correctly.
        """
        print("Setup: Mock parser with usage event...")
        mock_parser.feed.return_value = [
            {"type": "content", "data": "text"},
            {"type": "usage", "data": {"credits": 0.002}}
        ]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Collecting stream...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with patch('kiro.streaming_core.parse_bracket_tool_calls', return_value=[]):
                    result = await collect_stream_to_result(mock_response, first_token_timeout=30)
        
        print(f"Collected usage: {result.usage}")
        assert result.usage == {"credits": 0.002}
        print("✓ Usage collected correctly")
    
    @pytest.mark.asyncio
    async def test_collects_context_usage_percentage(self, mock_response, mock_parser):
        """
        What it does: Collects context usage percentage from stream.
        Goal: Verify context usage is stored correctly.
        """
        print("Setup: Mock parser with context_usage event...")
        mock_parser.feed.return_value = [
            {"type": "content", "data": "text"},
            {"type": "context_usage", "data": 8.5}
        ]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Collecting stream...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with patch('kiro.streaming_core.parse_bracket_tool_calls', return_value=[]):
                    result = await collect_stream_to_result(mock_response, first_token_timeout=30)
        
        print(f"Collected context_usage_percentage: {result.context_usage_percentage}")
        assert result.context_usage_percentage == 8.5
        print("✓ Context usage percentage collected correctly")
    
    @pytest.mark.asyncio
    async def test_collects_thinking_content(self, mock_response, mock_parser):
        """
        What it does: Collects thinking content from stream.
        Goal: Verify thinking content is accumulated correctly.
        """
        print("Setup: Mock parser with thinking content...")
        # We need to mock the thinking parser behavior
        mock_parser.feed.return_value = [{"type": "content", "data": "thinking text"}]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        # Create mock events that include thinking
        mock_events = [
            KiroEvent(type="thinking", thinking_content="Let me think..."),
            KiroEvent(type="content", content="Here is my answer")
        ]
        
        async def mock_parse_kiro_stream(*args, **kwargs):
            for event in mock_events:
                yield event
        
        print("Action: Collecting stream with thinking...")
        
        with patch('kiro.streaming_core.parse_kiro_stream', mock_parse_kiro_stream):
            with patch('kiro.streaming_core.parse_bracket_tool_calls', return_value=[]):
                result = await collect_stream_to_result(mock_response, first_token_timeout=30)
        
        print(f"Collected thinking_content: '{result.thinking_content}'")
        print(f"Collected content: '{result.content}'")
        assert result.thinking_content == "Let me think..."
        assert result.content == "Here is my answer"
        print("✓ Thinking content collected correctly")
    
    @pytest.mark.asyncio
    async def test_deduplicates_bracket_tool_calls(self, mock_response, mock_parser):
        """
        What it does: Deduplicates bracket-style tool calls.
        Goal: Verify duplicate tool calls are removed.
        """
        print("Setup: Mock parser with tool calls and bracket tool calls...")
        mock_parser.feed.return_value = [{"type": "content", "data": "text"}]
        mock_parser.get_tool_calls.return_value = [
            {"id": "call_1", "function": {"name": "func1", "arguments": "{}"}}
        ]
        
        bracket_tool_calls = [
            {"id": "call_1", "function": {"name": "func1", "arguments": "{}"}},  # Duplicate
            {"id": "call_2", "function": {"name": "func2", "arguments": "{}"}}   # New
        ]
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Collecting stream with duplicates...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with patch('kiro.streaming_core.parse_bracket_tool_calls', return_value=bracket_tool_calls):
                    with patch('kiro.streaming_core.deduplicate_tool_calls') as mock_dedup:
                        mock_dedup.return_value = [
                            {"id": "call_1", "function": {"name": "func1", "arguments": "{}"}},
                            {"id": "call_2", "function": {"name": "func2", "arguments": "{}"}}
                        ]
                        result = await collect_stream_to_result(mock_response, first_token_timeout=30)
        
        print(f"Collected tool calls: {len(result.tool_calls)}")
        assert len(result.tool_calls) == 2
        print("✓ Tool calls deduplicated correctly")


# ==================================================================================================
# Tests for calculate_tokens_from_context_usage()
# ==================================================================================================

class TestCalculateTokensFromContextUsage:
    """Tests for calculate_tokens_from_context_usage() function."""
    
    def test_calculates_tokens_from_percentage(self, mock_model_cache):
        """
        What it does: Calculates tokens from context usage percentage.
        Goal: Verify token calculation is correct.
        """
        print("Setup: Context usage 10% with 200000 max tokens...")
        context_usage_percentage = 10.0
        completion_tokens = 100
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-sonnet-4"
        )
        
        # 10% of 200000 = 20000 total tokens
        # prompt_tokens = 20000 - 100 = 19900
        print(f"Comparing total_tokens: Expected 20000, Got {total_tokens}")
        assert total_tokens == 20000
        print(f"Comparing prompt_tokens: Expected 19900, Got {prompt_tokens}")
        assert prompt_tokens == 19900
        assert prompt_source == "subtraction"
        assert total_source == "API Kiro"
        print("✓ Tokens calculated correctly")
    
    def test_handles_zero_percentage(self, mock_model_cache):
        """
        What it does: Handles zero context usage percentage.
        Goal: Verify fallback behavior for zero percentage.
        """
        print("Setup: Context usage 0%...")
        context_usage_percentage = 0.0
        completion_tokens = 100
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-sonnet-4"
        )
        
        print(f"Comparing prompt_tokens: Expected 0, Got {prompt_tokens}")
        assert prompt_tokens == 0
        print(f"Comparing total_tokens: Expected 100, Got {total_tokens}")
        assert total_tokens == 100
        assert prompt_source == "unknown"
        assert total_source == "tiktoken"
        print("✓ Zero percentage handled correctly")
    
    def test_handles_none_percentage(self, mock_model_cache):
        """
        What it does: Handles None context usage percentage.
        Goal: Verify fallback behavior for None percentage.
        """
        print("Setup: Context usage None...")
        context_usage_percentage = None
        completion_tokens = 100
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-sonnet-4"
        )
        
        print(f"Comparing prompt_tokens: Expected 0, Got {prompt_tokens}")
        assert prompt_tokens == 0
        print(f"Comparing total_tokens: Expected 100, Got {total_tokens}")
        assert total_tokens == 100
        assert prompt_source == "unknown"
        assert total_source == "tiktoken"
        print("✓ None percentage handled correctly")
    
    def test_prevents_negative_prompt_tokens(self, mock_model_cache):
        """
        What it does: Prevents negative prompt tokens.
        Goal: Verify prompt_tokens is never negative.
        """
        print("Setup: Very small context usage with large completion...")
        context_usage_percentage = 0.01  # 0.01% of 200000 = 20 total tokens
        completion_tokens = 100  # More than total!
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-sonnet-4"
        )
        
        print(f"Comparing prompt_tokens: Expected >= 0, Got {prompt_tokens}")
        assert prompt_tokens >= 0
        print("✓ Negative prompt tokens prevented")
    
    def test_uses_model_specific_max_tokens(self, mock_model_cache):
        """
        What it does: Uses model-specific max input tokens.
        Goal: Verify model cache is queried correctly.
        """
        print("Setup: Different max tokens for model...")
        mock_model_cache.get_max_input_tokens.return_value = 100000  # Different from default
        context_usage_percentage = 10.0
        completion_tokens = 100
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-haiku-3"
        )
        
        # 10% of 100000 = 10000 total tokens
        print(f"Comparing total_tokens: Expected 10000, Got {total_tokens}")
        assert total_tokens == 10000
        
        # Verify model cache was called with correct model
        mock_model_cache.get_max_input_tokens.assert_called_with("claude-haiku-3")
        print("✓ Model-specific max tokens used correctly")
    
    def test_small_percentage_calculation(self, mock_model_cache):
        """
        What it does: Calculates tokens for small percentage.
        Goal: Verify precision for small percentages.
        """
        print("Setup: Context usage 0.5%...")
        context_usage_percentage = 0.5
        completion_tokens = 50
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-sonnet-4"
        )
        
        # 0.5% of 200000 = 1000 total tokens
        # prompt_tokens = 1000 - 50 = 950
        print(f"Comparing total_tokens: Expected 1000, Got {total_tokens}")
        assert total_tokens == 1000
        print(f"Comparing prompt_tokens: Expected 950, Got {prompt_tokens}")
        assert prompt_tokens == 950
        print("✓ Small percentage calculated correctly")
    
    def test_large_percentage_calculation(self, mock_model_cache):
        """
        What it does: Calculates tokens for large percentage.
        Goal: Verify calculation for high context usage.
        """
        print("Setup: Context usage 95%...")
        context_usage_percentage = 95.0
        completion_tokens = 1000
        
        print("Action: Calculating tokens...")
        prompt_tokens, total_tokens, prompt_source, total_source = calculate_tokens_from_context_usage(
            context_usage_percentage, completion_tokens, mock_model_cache, "claude-sonnet-4"
        )
        
        # 95% of 200000 = 190000 total tokens
        # prompt_tokens = 190000 - 1000 = 189000
        print(f"Comparing total_tokens: Expected 190000, Got {total_tokens}")
        assert total_tokens == 190000
        print(f"Comparing prompt_tokens: Expected 189000, Got {prompt_tokens}")
        assert prompt_tokens == 189000
        print("✓ Large percentage calculated correctly")


# ==================================================================================================
# Tests for thinking parser integration
# ==================================================================================================

class TestThinkingParserIntegration:
    """Tests for thinking parser integration in streaming."""
    
    @pytest.mark.asyncio
    async def test_thinking_parser_enabled_when_fake_reasoning_on(self, mock_response, mock_parser):
        """
        What it does: Enables thinking parser when FAKE_REASONING_ENABLED is True.
        Goal: Verify thinking parser is created.
        """
        print("Setup: Enable fake reasoning...")
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream with fake reasoning enabled...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', True):
                with patch('kiro.streaming_core.ThinkingParser') as mock_thinking_parser_class:
                    mock_thinking_parser = MagicMock()
                    mock_thinking_parser.feed.return_value = MagicMock(
                        thinking_content=None,
                        regular_content="Hello",
                        is_first_thinking_chunk=False,
                        is_last_thinking_chunk=False
                    )
                    mock_thinking_parser.finalize.return_value = MagicMock(
                        thinking_content=None,
                        regular_content=None,
                        is_first_thinking_chunk=False,
                        is_last_thinking_chunk=False
                    )
                    mock_thinking_parser.found_thinking_block = False
                    mock_thinking_parser_class.return_value = mock_thinking_parser
                    
                    async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                        events.append(event)
                    
                    # Verify ThinkingParser was instantiated
                    mock_thinking_parser_class.assert_called_once()
        
        print("✓ Thinking parser enabled when fake reasoning is on")
    
    @pytest.mark.asyncio
    async def test_thinking_parser_disabled_when_fake_reasoning_off(self, mock_response, mock_parser):
        """
        What it does: Disables thinking parser when FAKE_REASONING_ENABLED is False.
        Goal: Verify thinking parser is not created.
        """
        print("Setup: Disable fake reasoning...")
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream with fake reasoning disabled...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with patch('kiro.streaming_core.ThinkingParser') as mock_thinking_parser_class:
                    async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                        events.append(event)
                    
                    # Verify ThinkingParser was NOT instantiated
                    mock_thinking_parser_class.assert_not_called()
        
        print("✓ Thinking parser disabled when fake reasoning is off")
    
    @pytest.mark.asyncio
    async def test_thinking_parser_can_be_disabled_via_parameter(self, mock_response, mock_parser):
        """
        What it does: Disables thinking parser via enable_thinking_parser parameter.
        Goal: Verify parameter overrides config.
        """
        print("Setup: Enable fake reasoning but disable via parameter...")
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        mock_parser.get_tool_calls.return_value = []
        
        async def mock_aiter_bytes():
            yield b'chunk1'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        print("Action: Parsing stream with thinking parser disabled via parameter...")
        events = []
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', True):
                with patch('kiro.streaming_core.ThinkingParser') as mock_thinking_parser_class:
                    async for event in parse_kiro_stream(
                        mock_response,
                        first_token_timeout=30,
                        enable_thinking_parser=False
                    ):
                        events.append(event)
                    
                    # Verify ThinkingParser was NOT instantiated
                    mock_thinking_parser_class.assert_not_called()
        
        print("✓ Thinking parser disabled via parameter")


# ==================================================================================================
# Tests for error handling
# ==================================================================================================

class TestStreamingCoreErrorHandling:
    """Tests for error handling in streaming_core."""
    
    @pytest.mark.asyncio
    async def test_propagates_first_token_timeout_error(self, mock_response):
        """
        What it does: Propagates FirstTokenTimeoutError.
        Goal: Verify timeout error is not caught internally.
        """
        print("Setup: Mock response that times out...")
        
        async def mock_aiter_bytes():
            yield b'chunk'
        
        mock_response.aiter_bytes = mock_aiter_bytes
        
        async def mock_wait_for_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()
        
        print("Action: Parsing stream with timeout...")
        
        with patch('kiro.streaming_core.asyncio.wait_for', side_effect=mock_wait_for_timeout):
            with pytest.raises(FirstTokenTimeoutError):
                async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                    pass
        
        print("✓ FirstTokenTimeoutError propagated correctly")
    
    @pytest.mark.asyncio
    async def test_propagates_generator_exit(self, mock_response, mock_parser):
        """
        What it does: Propagates GeneratorExit.
        Goal: Verify client disconnect is handled.
        """
        print("Setup: Mock response that raises GeneratorExit...")
        
        async def mock_aiter_bytes():
            yield b'chunk1'
            raise GeneratorExit()
        
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        
        print("Action: Parsing stream with GeneratorExit...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with pytest.raises(GeneratorExit):
                    async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                        pass
        
        print("✓ GeneratorExit propagated correctly")
    
    @pytest.mark.asyncio
    async def test_propagates_other_exceptions(self, mock_response, mock_parser):
        """
        What it does: Propagates other exceptions.
        Goal: Verify errors are not swallowed.
        """
        print("Setup: Mock response that raises RuntimeError...")
        
        async def mock_aiter_bytes():
            yield b'chunk1'
            raise RuntimeError("Test error")
        
        mock_response.aiter_bytes = mock_aiter_bytes
        mock_parser.feed.return_value = [{"type": "content", "data": "Hello"}]
        
        print("Action: Parsing stream with RuntimeError...")
        
        with patch('kiro.streaming_core.AwsEventStreamParser', return_value=mock_parser):
            with patch('kiro.streaming_core.FAKE_REASONING_ENABLED', False):
                with pytest.raises(RuntimeError) as exc_info:
                    async for event in parse_kiro_stream(mock_response, first_token_timeout=30):
                        pass
        
        print(f"Caught exception: {exc_info.value}")
        assert "Test error" in str(exc_info.value)
        print("✓ RuntimeError propagated correctly")