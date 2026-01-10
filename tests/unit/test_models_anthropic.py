# -*- coding: utf-8 -*-

"""
Unit tests for Anthropic Pydantic models.

Tests for image-related models added in Issue #30 fix:
- Base64ImageSource
- URLImageSource
- ImageContentBlock
- ContentBlock union with ImageContentBlock
- AnthropicMessage with image content
"""

import pytest
from pydantic import ValidationError

from kiro.models_anthropic import (
    Base64ImageSource,
    URLImageSource,
    ImageContentBlock,
    ContentBlock,
    TextContentBlock,
    ToolUseContentBlock,
    ToolResultContentBlock,
    AnthropicMessage,
    AnthropicMessagesRequest,
)


# Base64 1x1 pixel JPEG for testing
TEST_IMAGE_BASE64 = "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="


# ==================================================================================================
# Tests for Base64ImageSource
# ==================================================================================================

class TestBase64ImageSource:
    """Tests for Base64ImageSource Pydantic model."""
    
    def test_valid_base64_source(self):
        """
        What it does: Verifies creation of valid Base64ImageSource.
        Purpose: Ensure model accepts valid base64 image data.
        """
        print("Setup: Creating Base64ImageSource with valid data...")
        source = Base64ImageSource(
            type="base64",
            media_type="image/jpeg",
            data=TEST_IMAGE_BASE64
        )
        
        print(f"Result: {source}")
        print(f"Comparing type: Expected 'base64', Got '{source.type}'")
        assert source.type == "base64"
        
        print(f"Comparing media_type: Expected 'image/jpeg', Got '{source.media_type}'")
        assert source.media_type == "image/jpeg"
        
        print(f"Comparing data: Expected {TEST_IMAGE_BASE64[:20]}..., Got {source.data[:20]}...")
        assert source.data == TEST_IMAGE_BASE64
    
    def test_type_defaults_to_base64(self):
        """
        What it does: Verifies that type defaults to "base64".
        Purpose: Ensure default value is set correctly.
        """
        print("Setup: Creating Base64ImageSource without explicit type...")
        source = Base64ImageSource(
            media_type="image/png",
            data=TEST_IMAGE_BASE64
        )
        
        print(f"Comparing type: Expected 'base64', Got '{source.type}'")
        assert source.type == "base64"
    
    def test_requires_media_type(self):
        """
        What it does: Verifies that media_type is required.
        Purpose: Ensure validation fails without media_type.
        """
        print("Setup: Attempting to create Base64ImageSource without media_type...")
        
        print("Action: Creating model (should raise ValidationError)...")
        with pytest.raises(ValidationError) as exc_info:
            Base64ImageSource(data=TEST_IMAGE_BASE64)
        
        print(f"ValidationError raised: {exc_info.value}")
        assert "media_type" in str(exc_info.value)
    
    def test_requires_data(self):
        """
        What it does: Verifies that data is required.
        Purpose: Ensure validation fails without data.
        """
        print("Setup: Attempting to create Base64ImageSource without data...")
        
        print("Action: Creating model (should raise ValidationError)...")
        with pytest.raises(ValidationError) as exc_info:
            Base64ImageSource(media_type="image/jpeg")
        
        print(f"ValidationError raised: {exc_info.value}")
        assert "data" in str(exc_info.value)
    
    def test_accepts_various_media_types(self):
        """
        What it does: Verifies acceptance of various image media types.
        Purpose: Ensure all common image formats are supported.
        """
        print("Setup: Testing various media types...")
        media_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]
        
        for media_type in media_types:
            print(f"Testing media_type: {media_type}")
            source = Base64ImageSource(media_type=media_type, data=TEST_IMAGE_BASE64)
            assert source.media_type == media_type
        
        print("All media types accepted successfully")


# ==================================================================================================
# Tests for URLImageSource
# ==================================================================================================

class TestURLImageSource:
    """Tests for URLImageSource Pydantic model."""
    
    def test_valid_url_source(self):
        """
        What it does: Verifies creation of valid URLImageSource.
        Purpose: Ensure model accepts valid URL.
        """
        print("Setup: Creating URLImageSource with valid URL...")
        source = URLImageSource(
            type="url",
            url="https://example.com/image.jpg"
        )
        
        print(f"Result: {source}")
        print(f"Comparing type: Expected 'url', Got '{source.type}'")
        assert source.type == "url"
        
        print(f"Comparing url: Expected 'https://example.com/image.jpg', Got '{source.url}'")
        assert source.url == "https://example.com/image.jpg"
    
    def test_type_defaults_to_url(self):
        """
        What it does: Verifies that type defaults to "url".
        Purpose: Ensure default value is set correctly.
        """
        print("Setup: Creating URLImageSource without explicit type...")
        source = URLImageSource(url="https://example.com/image.png")
        
        print(f"Comparing type: Expected 'url', Got '{source.type}'")
        assert source.type == "url"
    
    def test_requires_url(self):
        """
        What it does: Verifies that url is required.
        Purpose: Ensure validation fails without url.
        """
        print("Setup: Attempting to create URLImageSource without url...")
        
        print("Action: Creating model (should raise ValidationError)...")
        with pytest.raises(ValidationError) as exc_info:
            URLImageSource()
        
        print(f"ValidationError raised: {exc_info.value}")
        assert "url" in str(exc_info.value)


# ==================================================================================================
# Tests for ImageContentBlock
# ==================================================================================================

class TestImageContentBlock:
    """Tests for ImageContentBlock Pydantic model."""
    
    def test_with_base64_source(self):
        """
        What it does: Verifies creation of ImageContentBlock with base64 source.
        Purpose: Ensure model accepts Base64ImageSource.
        """
        print("Setup: Creating ImageContentBlock with base64 source...")
        block = ImageContentBlock(
            type="image",
            source=Base64ImageSource(
                media_type="image/jpeg",
                data=TEST_IMAGE_BASE64
            )
        )
        
        print(f"Result: {block}")
        print(f"Comparing type: Expected 'image', Got '{block.type}'")
        assert block.type == "image"
        
        print(f"Comparing source.type: Expected 'base64', Got '{block.source.type}'")
        assert block.source.type == "base64"
        assert block.source.media_type == "image/jpeg"
    
    def test_with_url_source(self):
        """
        What it does: Verifies creation of ImageContentBlock with URL source.
        Purpose: Ensure model accepts URLImageSource.
        """
        print("Setup: Creating ImageContentBlock with URL source...")
        block = ImageContentBlock(
            type="image",
            source=URLImageSource(url="https://example.com/image.jpg")
        )
        
        print(f"Result: {block}")
        print(f"Comparing type: Expected 'image', Got '{block.type}'")
        assert block.type == "image"
        
        print(f"Comparing source.type: Expected 'url', Got '{block.source.type}'")
        assert block.source.type == "url"
        assert block.source.url == "https://example.com/image.jpg"
    
    def test_with_dict_base64_source(self):
        """
        What it does: Verifies creation of ImageContentBlock with dict source.
        Purpose: Ensure model accepts dict that matches Base64ImageSource schema.
        """
        print("Setup: Creating ImageContentBlock with dict source...")
        block = ImageContentBlock(
            type="image",
            source={
                "type": "base64",
                "media_type": "image/png",
                "data": TEST_IMAGE_BASE64
            }
        )
        
        print(f"Result: {block}")
        print(f"Comparing source.type: Expected 'base64', Got '{block.source.type}'")
        assert block.source.type == "base64"
        assert block.source.media_type == "image/png"
    
    def test_with_dict_url_source(self):
        """
        What it does: Verifies creation of ImageContentBlock with dict URL source.
        Purpose: Ensure model accepts dict that matches URLImageSource schema.
        """
        print("Setup: Creating ImageContentBlock with dict URL source...")
        block = ImageContentBlock(
            type="image",
            source={
                "type": "url",
                "url": "https://example.com/test.gif"
            }
        )
        
        print(f"Result: {block}")
        print(f"Comparing source.type: Expected 'url', Got '{block.source.type}'")
        assert block.source.type == "url"
        assert block.source.url == "https://example.com/test.gif"
    
    def test_type_literal_is_image(self):
        """
        What it does: Verifies that type must be "image".
        Purpose: Ensure type literal validation works.
        """
        print("Setup: Creating ImageContentBlock with correct type...")
        block = ImageContentBlock(
            source=Base64ImageSource(media_type="image/jpeg", data=TEST_IMAGE_BASE64)
        )
        
        print(f"Comparing type: Expected 'image', Got '{block.type}'")
        assert block.type == "image"
    
    def test_requires_source(self):
        """
        What it does: Verifies that source is required.
        Purpose: Ensure validation fails without source.
        """
        print("Setup: Attempting to create ImageContentBlock without source...")
        
        print("Action: Creating model (should raise ValidationError)...")
        with pytest.raises(ValidationError) as exc_info:
            ImageContentBlock(type="image")
        
        print(f"ValidationError raised: {exc_info.value}")
        assert "source" in str(exc_info.value)


# ==================================================================================================
# Tests for ContentBlock Union
# ==================================================================================================

class TestContentBlockUnion:
    """Tests for ContentBlock union type accepting ImageContentBlock."""
    
    def test_accepts_text_content_block(self):
        """
        What it does: Verifies ContentBlock accepts TextContentBlock.
        Purpose: Ensure union includes text blocks.
        """
        print("Setup: Creating TextContentBlock...")
        block: ContentBlock = TextContentBlock(text="Hello, world!")
        
        print(f"Result: {block}")
        print(f"Comparing type: Expected 'text', Got '{block.type}'")
        assert block.type == "text"
        assert block.text == "Hello, world!"
    
    def test_accepts_image_content_block(self):
        """
        What it does: Verifies ContentBlock accepts ImageContentBlock.
        Purpose: Ensure union includes image blocks (Issue #30 fix).
        
        This is the key test that verifies the fix for Issue #30.
        Before the fix, ContentBlock union did not include ImageContentBlock,
        causing 422 Validation Error when image content was sent.
        """
        print("Setup: Creating ImageContentBlock...")
        block: ContentBlock = ImageContentBlock(
            source=Base64ImageSource(media_type="image/jpeg", data=TEST_IMAGE_BASE64)
        )
        
        print(f"Result: {block}")
        print(f"Comparing type: Expected 'image', Got '{block.type}'")
        assert block.type == "image"
        assert block.source.type == "base64"
    
    def test_accepts_tool_use_content_block(self):
        """
        What it does: Verifies ContentBlock accepts ToolUseContentBlock.
        Purpose: Ensure union includes tool_use blocks.
        """
        print("Setup: Creating ToolUseContentBlock...")
        block: ContentBlock = ToolUseContentBlock(
            id="call_123",
            name="get_weather",
            input={"location": "Moscow"}
        )
        
        print(f"Result: {block}")
        print(f"Comparing type: Expected 'tool_use', Got '{block.type}'")
        assert block.type == "tool_use"
    
    def test_accepts_tool_result_content_block(self):
        """
        What it does: Verifies ContentBlock accepts ToolResultContentBlock.
        Purpose: Ensure union includes tool_result blocks.
        """
        print("Setup: Creating ToolResultContentBlock...")
        block: ContentBlock = ToolResultContentBlock(
            tool_use_id="call_123",
            content="Weather: Sunny, 25°C"
        )
        
        print(f"Result: {block}")
        print(f"Comparing type: Expected 'tool_result', Got '{block.type}'")
        assert block.type == "tool_result"


# ==================================================================================================
# Tests for AnthropicMessage with Image Content (Issue #30 fix verification)
# ==================================================================================================

class TestAnthropicMessageWithImages:
    """
    Tests for AnthropicMessage with image content.
    
    These tests verify the fix for Issue #30 - 422 Validation Error
    when sending image content blocks in messages.
    """
    
    def test_message_with_image_content_validates(self):
        """
        What it does: Verifies AnthropicMessage accepts image content blocks.
        Purpose: This is the PRIMARY test for Issue #30 fix.
        
        Before the fix, this would raise a ValidationError because
        ContentBlock union did not include ImageContentBlock.
        """
        print("Setup: Creating AnthropicMessage with image content...")
        message = AnthropicMessage(
            role="user",
            content=[
                TextContentBlock(text="What's in this image?"),
                ImageContentBlock(
                    source=Base64ImageSource(
                        media_type="image/jpeg",
                        data=TEST_IMAGE_BASE64
                    )
                )
            ]
        )
        
        print(f"Result: {message}")
        print(f"Comparing role: Expected 'user', Got '{message.role}'")
        assert message.role == "user"
        
        print(f"Comparing content length: Expected 2, Got {len(message.content)}")
        assert len(message.content) == 2
        
        print(f"Comparing content[0].type: Expected 'text', Got '{message.content[0].type}'")
        assert message.content[0].type == "text"
        
        print(f"Comparing content[1].type: Expected 'image', Got '{message.content[1].type}'")
        assert message.content[1].type == "image"
    
    def test_message_with_dict_image_content_validates(self):
        """
        What it does: Verifies AnthropicMessage accepts dict image content.
        Purpose: Ensure raw dict format (as received from API) validates correctly.
        
        This is how the actual API request comes in - as raw dicts, not Pydantic models.
        """
        print("Setup: Creating AnthropicMessage with dict image content...")
        message = AnthropicMessage(
            role="user",
            content=[
                {"type": "text", "text": "Describe this image"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": TEST_IMAGE_BASE64
                    }
                }
            ]
        )
        
        print(f"Result: {message}")
        print(f"Comparing content length: Expected 2, Got {len(message.content)}")
        assert len(message.content) == 2
        
        print(f"Comparing content[1].type: Expected 'image', Got '{message.content[1].type}'")
        assert message.content[1].type == "image"
        assert message.content[1].source.type == "base64"
    
    def test_message_with_multiple_images_validates(self):
        """
        What it does: Verifies AnthropicMessage accepts multiple images.
        Purpose: Ensure multiple image blocks in one message work correctly.
        """
        print("Setup: Creating AnthropicMessage with multiple images...")
        message = AnthropicMessage(
            role="user",
            content=[
                {"type": "text", "text": "Compare these images"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": TEST_IMAGE_BASE64}
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": TEST_IMAGE_BASE64}
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/webp", "data": TEST_IMAGE_BASE64}
                }
            ]
        )
        
        print(f"Result content length: {len(message.content)}")
        assert len(message.content) == 4
        
        image_blocks = [b for b in message.content if b.type == "image"]
        print(f"Image blocks count: {len(image_blocks)}")
        assert len(image_blocks) == 3
    
    def test_message_with_url_image_validates(self):
        """
        What it does: Verifies AnthropicMessage accepts URL image source.
        Purpose: Ensure URL-based images are accepted (even if not fully supported).
        """
        print("Setup: Creating AnthropicMessage with URL image...")
        message = AnthropicMessage(
            role="user",
            content=[
                {"type": "text", "text": "What's in this image?"},
                {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": "https://example.com/image.jpg"
                    }
                }
            ]
        )
        
        print(f"Result: {message}")
        print(f"Comparing content[1].source.type: Expected 'url', Got '{message.content[1].source.type}'")
        assert message.content[1].source.type == "url"
        assert message.content[1].source.url == "https://example.com/image.jpg"


# ==================================================================================================
# Tests for AnthropicMessagesRequest with Image Content
# ==================================================================================================

class TestAnthropicMessagesRequestWithImages:
    """Tests for full AnthropicMessagesRequest with image content."""
    
    def test_request_with_image_message_validates(self):
        """
        What it does: Verifies full request with image content validates.
        Purpose: End-to-end validation test for Issue #30 fix.
        
        This simulates the actual request that was failing with 422 error.
        """
        print("Setup: Creating full AnthropicMessagesRequest with image...")
        request = AnthropicMessagesRequest(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[
                AnthropicMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": TEST_IMAGE_BASE64
                            }
                        }
                    ]
                )
            ]
        )
        
        print(f"Result: {request}")
        print(f"Comparing model: Expected 'claude-sonnet-4-5', Got '{request.model}'")
        assert request.model == "claude-sonnet-4-5"
        
        print(f"Comparing messages count: Expected 1, Got {len(request.messages)}")
        assert len(request.messages) == 1
        
        print(f"Comparing content count: Expected 2, Got {len(request.messages[0].content)}")
        assert len(request.messages[0].content) == 2
        
        print("Request with image content validated successfully!")
    
    def test_request_with_conversation_including_images(self):
        """
        What it does: Verifies multi-turn conversation with images validates.
        Purpose: Ensure images work in conversation context.
        """
        print("Setup: Creating multi-turn conversation with images...")
        request = AnthropicMessagesRequest(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[
                AnthropicMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": TEST_IMAGE_BASE64
                            }
                        }
                    ]
                ),
                AnthropicMessage(
                    role="assistant",
                    content="I can see a small test image."
                ),
                AnthropicMessage(
                    role="user",
                    content="Can you describe it in more detail?"
                )
            ]
        )
        
        print(f"Result messages count: {len(request.messages)}")
        assert len(request.messages) == 3
        
        # First message has image
        assert request.messages[0].content[1].type == "image"
        
        # Second message is string (assistant)
        assert request.messages[1].content == "I can see a small test image."
        
        # Third message is string (user follow-up)
        assert request.messages[2].content == "Can you describe it in more detail?"
        
        print("Multi-turn conversation with images validated successfully!")
