"""Content rendering for the streaming UI.

COMPATIBILITY SHIM: This module preserves the old string-returning API
for any external code that imported render_* functions directly.
New code should use rich_output.py (print_* functions) instead.

The string-returning functions now capture Rich Console output into strings.
The status bar function remains unchanged (returns plain text).
"""

from typing import Any, Optional

from .cost import CostEstimate
from .rich_output import format_status_bar
from .state import Phase, SessionState

# Re-export the status bar formatter (still returns plain string)
render_status_bar = format_status_bar

# Re-export constants that external code might reference
from .rich_output import (
    BOX_CORNER_BL,
    BOX_CORNER_TL,
    BOX_HORIZONTAL,
    BOX_VERTICAL,
    CHECK,
    CROSS,
)

# Default truncation limits (kept for backward compat)
MAX_TOOL_OUTPUT_LINES = 10
MAX_TOOL_INPUT_CHARS = 200


def render_session_header(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_session_header

    print_session_header(state, cost)
    return ""


def render_session_footer(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_session_footer

    print_session_footer(state, cost)
    return ""


def render_tool_call(
    tool_name: str,
    tool_input: dict[str, Any],
    depth: int = 0,
) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_tool_call

    print_tool_call(tool_name, tool_input, depth)
    return ""


def render_tool_result(
    tool_name: str,
    result: Any,
    success: bool = True,
    depth: int = 0,
    max_lines: int = MAX_TOOL_OUTPUT_LINES,
) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_tool_result

    print_tool_result(tool_name, result, success, depth, max_lines)
    return ""


def render_thinking_block(text: str, depth: int = 0) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_thinking_block

    print_thinking_block(text, depth)
    return ""


def render_thinking_elapsed(seconds: float, depth: int = 0) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_thinking_elapsed

    print_thinking_elapsed(seconds, depth)
    return ""


def render_token_usage(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_create: int = 0,
    elapsed: str = "",
    cost: Optional[CostEstimate] = None,
    depth: int = 0,
) -> str:
    """Backward-compat: returns empty string, prints via Rich Console."""
    from .rich_output import print_token_usage

    print_token_usage(
        input_tokens, output_tokens, cache_read, cache_create, elapsed, cost, depth
    )
    return ""
