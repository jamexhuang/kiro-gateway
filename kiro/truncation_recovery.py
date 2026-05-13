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
Truncation recovery system for handling upstream Kiro API limitations.

Generates synthetic messages to inform the model about truncation.
ONLY activates when truncation is actually detected.

This module addresses Issue #56 - Kiro API truncates large tool call payloads
and content mid-stream. Since this is an upstream limitation that cannot be
prevented, we inform the model about the truncation so it can adapt its approach.
"""

from typing import Dict, Any

from loguru import logger


def should_inject_recovery() -> bool:
    """
    Check if truncation recovery is enabled.
    
    Returns:
        True if recovery should be injected, False otherwise
    """
    from kiro.config import TRUNCATION_RECOVERY
    return TRUNCATION_RECOVERY


def generate_truncation_tool_result(
    tool_name: str,
    tool_use_id: str,
    truncation_info: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Generate synthetic tool_result for truncated tool call.

    Message is carefully worded to:
    - Acknowledge API limitation (not model's fault)
    - Indicate whether partial content was preserved (repaired)
    - Guide model to complete the remaining content

    Args:
        tool_name: Name of the truncated tool
        tool_use_id: ID of the truncated tool call
        truncation_info: Diagnostic information about truncation

    Returns:
        Synthetic tool_result in unified format
    """
    was_repaired = truncation_info.get("repaired", False)

    if was_repaired:
        content = (
            "[API Limitation] Your tool call was truncated by the upstream API due to output size limits. "
            "The content was PARTIALLY delivered — the file was written with truncated content.\n\n"
            "Do NOT repeat the entire file. Instead, use the Edit tool to append or replace "
            "only the missing portion at the end of the file."
        )
        is_error = False  # Partial success, not a failure
    else:
        content = (
            "[API Limitation] Your tool call was truncated by the upstream API due to output size limits.\n\n"
            "If the tool result below shows an error or unexpected behavior, this is likely a CONSEQUENCE of the truncation, "
            "not the root cause. The tool call itself was cut off before it could be fully transmitted.\n\n"
            "Repeating the exact same operation will be truncated again. Consider adapting your approach."
        )
        is_error = True

    logger.debug(
        f"Generated synthetic tool_result for truncated tool '{tool_name}' "
        f"(id={tool_use_id}, {truncation_info['size_bytes']} bytes, {truncation_info['reason']}, repaired={was_repaired})"
    )

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error
    }


def generate_truncation_user_message() -> str:
    """
    Generate synthetic user message for content truncation.
    
    Message is carefully worded to:
    - Acknowledge it's not model's fault
    - Suggest adaptation without specific instructions
    - NOT tell model to "break into steps" (causes micro-steps)
    
    Returns:
        Synthetic user message text
    
    Example:
        >>> generate_truncation_user_message()
        '[System Notice] Your previous response was truncated...'
    """
    return (
        "[System Notice] Your previous response was truncated by the API due to "
        "output size limitations. This is not an error on your part. "
        "If you need to continue, please adapt your approach rather than repeating the same output."
    )
