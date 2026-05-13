# -*- coding: utf-8 -*-

"""
Unit tests for image eviction from conversation history.
"""

import json
import os
from unittest.mock import patch

from kiro.image_eviction import evict_images_from_history


def _make_image(size_kb: int = 200) -> dict:
    """Create a fake Kiro-format image of approximately size_kb kilobytes."""
    # base64 chars: ~1.33x raw bytes, so for size_kb KB of JSON we need ~size_kb*750 chars
    fake_b64 = "A" * (size_kb * 750)
    return {"format": "png", "source": {"bytes": fake_b64}}


def _make_user_entry(content: str = "hello", images: list = None) -> dict:
    """Create a Kiro-format userInputMessage history entry."""
    msg = {"content": content}
    if images:
        msg["images"] = images
    return {"userInputMessage": msg}


def _make_assistant_entry(content: str = "response") -> dict:
    """Create a Kiro-format assistantResponseMessage history entry."""
    return {"assistantResponseMessage": {"content": content}}


class TestImageEvictionDisabled:
    """Tests when image eviction is disabled."""

    @patch("kiro.config.IMAGE_EVICTION_ENABLED", False)
    def test_disabled_does_nothing(self):
        """When disabled, no images are evicted."""
        history = [
            _make_user_entry("turn 1", [_make_image(200)]),
            _make_assistant_entry(),
            _make_user_entry("turn 2", [_make_image(200)]),
            _make_assistant_entry(),
            _make_user_entry("turn 3", [_make_image(200)]),
            _make_assistant_entry(),
        ]
        evicted, saved = evict_images_from_history(history)
        assert evicted == 0
        assert saved == 0
        # All images still present
        for entry in history:
            if "userInputMessage" in entry:
                assert "images" in entry["userInputMessage"]


class TestImageEvictionEnabled:
    """Tests when image eviction is enabled (default)."""

    def test_empty_history(self):
        """Empty history returns (0, 0)."""
        evicted, saved = evict_images_from_history([])
        assert evicted == 0
        assert saved == 0

    def test_no_images_in_history(self):
        """History without images returns (0, 0)."""
        history = [
            _make_user_entry("turn 1"),
            _make_assistant_entry(),
            _make_user_entry("turn 2"),
            _make_assistant_entry(),
        ]
        evicted, saved = evict_images_from_history(history)
        assert evicted == 0
        assert saved == 0

    def test_all_images_in_keep_window(self):
        """With 2 user messages and keep=2, no eviction."""
        history = [
            _make_user_entry("turn 1", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("turn 2", [_make_image(100)]),
            _make_assistant_entry(),
        ]
        evicted, saved = evict_images_from_history(history)
        assert evicted == 0
        assert saved == 0
        assert "images" in history[0]["userInputMessage"]
        assert "images" in history[2]["userInputMessage"]

    def test_evicts_old_images(self):
        """With 5 user messages and keep=2, evicts from first 3."""
        history = [
            _make_user_entry("turn 1", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("turn 2", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("turn 3", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("turn 4", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("turn 5", [_make_image(100)]),
            _make_assistant_entry(),
        ]
        evicted, saved = evict_images_from_history(history)
        assert evicted == 3  # turns 1, 2, 3 evicted
        assert saved > 0

        # First 3 user messages should NOT have images
        assert "images" not in history[0]["userInputMessage"]
        assert "images" not in history[2]["userInputMessage"]
        assert "images" not in history[4]["userInputMessage"]

        # Last 2 user messages should still have images
        assert "images" in history[6]["userInputMessage"]
        assert "images" in history[8]["userInputMessage"]

    def test_placeholder_appended(self):
        """Evicted entries have placeholder appended to content."""
        history = [
            _make_user_entry("original content", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("recent", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("most recent", [_make_image(100)]),
            _make_assistant_entry(),
        ]
        evict_images_from_history(history)

        content = history[0]["userInputMessage"]["content"]
        assert "original content" in content
        assert "[image was provided in earlier turn and has been processed]" in content

    def test_multiple_images_placeholder(self):
        """Multiple images in one message get a count in placeholder."""
        history = [
            _make_user_entry("pdf pages", [_make_image(100), _make_image(100), _make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("recent", [_make_image(100)]),
            _make_assistant_entry(),
            _make_user_entry("most recent", [_make_image(100)]),
            _make_assistant_entry(),
        ]
        evicted, saved = evict_images_from_history(history)
        assert evicted == 3  # 3 images from first message

        content = history[0]["userInputMessage"]["content"]
        assert "3 images" in content

    def test_bytes_saved_reasonable(self):
        """Bytes saved should be roughly proportional to image size."""
        history = [
            _make_user_entry("old", [_make_image(200)]),  # ~200KB
            _make_assistant_entry(),
            _make_user_entry("recent", [_make_image(50)]),
            _make_assistant_entry(),
            _make_user_entry("most recent", [_make_image(50)]),
            _make_assistant_entry(),
        ]
        evicted, saved = evict_images_from_history(history)
        assert evicted == 1
        # 200KB image should save at least 100KB
        assert saved > 100_000

    def test_user_message_without_images_not_affected(self):
        """User messages without images are not modified."""
        history = [
            _make_user_entry("no images here"),
            _make_assistant_entry(),
            _make_user_entry("also no images"),
            _make_assistant_entry(),
            _make_user_entry("recent with image", [_make_image(100)]),
            _make_assistant_entry(),
        ]
        evict_images_from_history(history)
        assert history[0]["userInputMessage"]["content"] == "no images here"
        assert history[2]["userInputMessage"]["content"] == "also no images"
