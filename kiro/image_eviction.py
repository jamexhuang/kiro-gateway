# -*- coding: utf-8 -*-

"""
Image eviction for Kiro API history payloads.

Replaces base64 image data in older history entries with lightweight
text placeholders, preserving conversation text context while reducing
payload size for image-heavy conversations (e.g., PDF reading).

When Claude Code reads a PDF, each page becomes a base64 PNG (100KB-500KB+).
Without eviction, these accumulate in history and quickly exceed the ~615KB
payload limit, triggering aggressive history trimming that removes valuable
text context.

This module evicts images from older turns (the model already processed them)
while keeping recent images intact.
"""

import json
from typing import Any, Dict, List, Tuple

from loguru import logger


def evict_images_from_history(history: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Evict images from older history entries in-place.

    Walks the history to identify user messages. Images in the most recent
    IMAGE_KEEP_LAST_TURNS user messages are preserved. All other images are
    removed and replaced with a text placeholder appended to the message content.

    Args:
        history: The Kiro-format history list (modified in-place).
                 Each entry is either {"userInputMessage": {...}} or
                 {"assistantResponseMessage": {...}}.

    Returns:
        Tuple of (images_evicted, bytes_saved_estimate).
    """
    from kiro.config import (
        IMAGE_EVICTION_ENABLED,
        IMAGE_KEEP_LAST_TURNS,
        IMAGE_EVICTION_PLACEHOLDER,
    )

    if not IMAGE_EVICTION_ENABLED:
        return 0, 0

    if not history:
        return 0, 0

    # Identify indices of userInputMessage entries (these define "turns")
    user_indices = [
        i for i, entry in enumerate(history)
        if "userInputMessage" in entry
    ]

    if not user_indices:
        return 0, 0

    # Determine which user message indices to KEEP images for
    keep_count = max(1, IMAGE_KEEP_LAST_TURNS)
    keep_indices = set(user_indices[-keep_count:])

    images_evicted = 0
    bytes_saved = 0

    for i, entry in enumerate(history):
        user_msg = entry.get("userInputMessage")
        if not user_msg:
            continue

        # Skip entries in the "keep" window
        if i in keep_indices:
            continue

        # Check if this user message has images
        images = user_msg.get("images")
        if not images:
            continue

        # Estimate bytes being removed
        for img in images:
            try:
                img_json = json.dumps(img, separators=(",", ":"))
                bytes_saved += len(img_json.encode("utf-8"))
            except (TypeError, ValueError):
                bytes_saved += 200000  # conservative estimate

        num_images = len(images)
        images_evicted += num_images

        # Remove images array
        del user_msg["images"]

        # Build placeholder text
        if num_images == 1:
            placeholder = IMAGE_EVICTION_PLACEHOLDER
        else:
            placeholder = f"[{num_images} images were provided in earlier turn and have been processed]"

        # Append placeholder to content
        current_content = user_msg.get("content", "")
        if current_content:
            user_msg["content"] = f"{current_content}\n\n{placeholder}"
        else:
            user_msg["content"] = placeholder

    if images_evicted > 0:
        logger.info(
            f"Image eviction: removed {images_evicted} image(s) from history, "
            f"saved ~{bytes_saved // 1024}KB"
        )

    return images_evicted, bytes_saved
