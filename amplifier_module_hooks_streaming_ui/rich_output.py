"""Rich Console rendering for the streaming UI.

Replaces raw ANSI escape code output with Rich renderables.
Provides semantic rendering functions that __init__.py calls
instead of the old renderer.py string builders.

Design constraints:
- No Rich Live (hooks are event-driven, not persistent render loops)
- Console writes to stderr (stdout is for program output / pipes)
- TTY auto-detection: rich markup when interactive, plain text when piped
- Thread-safe: all output goes through a single Console with lock
"""

import threading
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.theme import Theme

from .cost import CostEstimate
from .formatting import (
    CodeChange,
    extract_output,
    format_result_summary,
    format_tool_header,
    is_error_result,
)
from .state import SessionState

# ============================================================================
# Theme
# ============================================================================

AMPLIFIER_THEME = Theme(
    {
        "tool.header": "cyan",
        "tool.bullet": "cyan bold",
        "tool.result.ok": "green",
        "tool.result.error": "red bold",
        "tool.result.dim": "dim",
        "thinking.border": "dim",
        "thinking.text": "dim italic",
        "thinking.elapsed": "dim",
        "session.header": "bold",
        "session.sub": "bold cyan",
        "session.footer": "dim",
        "token.label": "dim",
        "token.cached": "dim green",
        "status.phase": "bold yellow",
        "status.info": "dim",
        "error": "bold red",
    }
)

# ============================================================================
# Console Singleton
# ============================================================================

_console: Optional[Console] = None
_lock = threading.Lock()


def get_console() -> Console:
    """Get or create the module-level Rich Console.

    Uses stderr so UI output doesn't pollute stdout (important for pipes).
    Auto-detects TTY: full rich markup when interactive, plain when piped.
    """
    global _console
    if _console is None:
        _console = Console(
            stderr=True,
            theme=AMPLIFIER_THEME,
            highlight=False,
        )
    return _console


def set_console(console: Console) -> None:
    """Override the module-level console (for testing)."""
    global _console
    _console = console


# ============================================================================
# Box Drawing Constants
# ============================================================================

BOX_CORNER_TL = "\u250c"
BOX_CORNER_BL = "\u2514"
BOX_VERTICAL = "\u2502"
BOX_HORIZONTAL = "\u2500"

BULLET_TRIANGLE = "\u25b8"  # small right-pointing triangle (claudechic style)
CHECK = "\u2713"
CROSS = "\u2717"

# Depth-based colors for agent tree
DEPTH_COLORS = ["cyan", "magenta", "green", "yellow", "blue"]


def _depth_prefix(depth: int) -> str:
    """Build a colored tree-branch prefix for the given nesting depth.

    depth=0 -> ""
    depth=1 -> "| "  (cyan)
    depth=2 -> "| | "  (cyan, magenta)
    """
    if depth == 0:
        return ""
    parts = []
    for d in range(1, depth + 1):
        color = DEPTH_COLORS[(d - 1) % len(DEPTH_COLORS)]
        parts.append(f"[{color}]{BOX_VERTICAL}[/] ")
    return "".join(parts)


# ============================================================================
# Session Rendering
# ============================================================================


def print_session_header(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> None:
    """Print session header -- agent tree style for nested sessions."""
    console = get_console()

    if state.depth > 0:
        prefix = _depth_prefix(state.depth)
        color = DEPTH_COLORS[(state.depth - 1) % len(DEPTH_COLORS)]
        agent_name = state.agent_name or _extract_agent_name(state.session_id)
        type_part = f" [dim]({state.agent_type})[/]" if state.agent_type else ""
        desc_part = f" [dim]\u2014 {state.agent_desc}[/]" if state.agent_desc else ""
        console.print(
            f"{prefix}[{color} bold]{BULLET_TRIANGLE} {agent_name}[/]{type_part}{desc_part}"
        )
    else:
        console.print()


def print_session_footer(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> None:
    """Print session footer -- compact summary for all sessions."""
    console = get_console()

    if state.depth == 0:
        # Root session: print a summary rule with key stats
        parts: list[str] = []

        if state.metrics.tool_calls > 0:
            parts.append(f"{state.metrics.tool_calls} tool calls")

        elapsed = state.elapsed_formatted()
        if elapsed:
            parts.append(f"\u23f1 {elapsed}")

        cost_str = cost.format() if cost else ""
        if cost_str:
            parts.append(cost_str)

        summary = " \u00b7 ".join(parts)
        if summary:
            console.print()
            console.print(f"[dim]\u2500 {summary}[/]")
        return

    # Nested session: agent completion line with depth prefix
    prefix = _depth_prefix(state.depth)
    color = DEPTH_COLORS[(state.depth - 1) % len(DEPTH_COLORS)]
    elapsed = state.elapsed_formatted()

    parts = [f"[{color}]{CHECK}[/] [{color}]Complete[/]"]

    if state.metrics.tool_calls > 0:
        parts.append(f"[dim]{state.metrics.tool_calls} tool calls[/]")

    parts.append(f"[dim]{BULLET_TRIANGLE} {elapsed}[/]")

    cost_str = cost.format() if cost else ""
    if cost_str:
        parts.append(f"[dim]{cost_str}[/]")

    console.print(f"{prefix}{'  '.join(parts)}")
    console.print()  # blank line after agent completion


# ============================================================================
# Tool Rendering
# ============================================================================


def print_tool_call(
    tool_name: str,
    tool_input: dict[str, Any],
    depth: int = 0,
    cwd: Path | None = None,
) -> None:
    """Print a tool invocation line (used for slow tools like Bash/delegate)."""
    console = get_console()
    prefix = _depth_prefix(depth)
    header = format_tool_header(tool_name, tool_input, cwd)
    console.print(f"{prefix}[tool.bullet]{BULLET_TRIANGLE}[/] [tool.header]{header}[/]")


def print_tool_result(
    tool_name: str,
    result: Any,
    success: bool = True,
    depth: int = 0,
    max_lines: int = 10,
) -> None:
    """Print a tool result -- used when header was already printed (slow tools)."""
    console = get_console()
    prefix = _depth_prefix(depth)
    is_err = not success or is_error_result(result)
    summary = format_result_summary(tool_name, result, is_error=is_err)

    if is_err:
        style = "tool.result.error"
    else:
        style = "tool.result.dim"

    if summary:
        console.print(f"{prefix}  [{style}]{summary}[/]")

    if is_err:
        output = extract_output(result)
        if output.strip():
            lines = output.strip().split("\n")
            show_lines = lines[:max_lines]
            for line in show_lines:
                console.print(f"{prefix}  [error]{line}[/]")
            if len(lines) > max_lines:
                hidden = len(lines) - max_lines
                console.print(f"{prefix}  [tool.result.dim]... +{hidden} lines[/]")


def print_tool_merged(
    header: str,
    tool_name: str,
    result: Any,
    success: bool = True,
    depth: int = 0,
    max_lines: int = 10,
) -> None:
    """Print a single merged line: header (result).

    Used for fast tools where we buffer the header and print
    everything at once in tool:post.
    """
    console = get_console()
    prefix = _depth_prefix(depth)
    is_err = not success or is_error_result(result)
    summary = format_result_summary(tool_name, result, is_error=is_err)

    if is_err:
        style = "tool.result.error"
    else:
        style = "tool.result.dim"

    result_part = f" [dim]\u2192[/] [{style}]{summary}[/]" if summary else ""
    console.print(
        f"{prefix}[tool.bullet]{BULLET_TRIANGLE}[/] [tool.header]{header}[/]{result_part}"
    )

    # For errors, still show detail lines below
    if is_err:
        output = extract_output(result)
        if output.strip():
            lines = output.strip().split("\n")
            show_lines = lines[:max_lines]
            for line in show_lines:
                console.print(f"{prefix}  [error]{line}[/]")
            if len(lines) > max_lines:
                hidden = len(lines) - max_lines
                console.print(f"{prefix}  [tool.result.dim]... +{hidden} lines[/]")


# ============================================================================
# Thinking Block Rendering
# ============================================================================


def print_thinking_block(text: str, depth: int = 0, max_preview_lines: int = 3) -> None:
    """Print a thinking block summary with optional preview.

    In compact mode (max_preview_lines=0), shows nothing (spinner covers it).
    With preview enabled, shows first N lines of the thinking text.
    """
    if not text or max_preview_lines <= 0:
        return

    console = get_console()
    prefix = _depth_prefix(depth)
    lines = text.strip().split("\n")
    show = lines[:max_preview_lines]

    for line in show:
        # Truncate long lines
        display = line[:120] + ("..." if len(line) > 120 else "")
        console.print(f"{prefix}  [thinking.text]{display}[/]")

    if len(lines) > max_preview_lines:
        remaining = len(lines) - max_preview_lines
        console.print(f"{prefix}  [thinking.elapsed]... +{remaining} lines[/]")


def print_thinking_start(depth: int = 0) -> None:
    """Print compact thinking indicator when reasoning begins."""
    console = get_console()
    prefix = _depth_prefix(depth)
    console.print(f"{prefix}[thinking.border]\u2847 Thinking...[/]")


def print_thinking_elapsed(seconds: float, depth: int = 0) -> None:
    """Print elapsed thinking time with check mark."""
    console = get_console()
    prefix = _depth_prefix(depth)

    if seconds < 60:
        time_str = f"{int(seconds)}s"
    else:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        time_str = f"{minutes}m {secs}s"

    console.print(f"{prefix}[thinking.elapsed]{CHECK} Reasoned for {time_str}[/]")


# ============================================================================
# Token Usage Rendering
# ============================================================================


def print_token_usage(
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_create: int = 0,
    elapsed: str = "",
    cost: Optional[CostEstimate] = None,
    depth: int = 0,
) -> None:
    """Print token usage summary line."""
    console = get_console()
    prefix = _depth_prefix(depth)

    total_input = input_tokens + cache_read + cache_create
    total = total_input + output_tokens

    parts: list[str] = []

    if cache_read > 0:
        cache_pct = int((cache_read / total_input) * 100) if total_input > 0 else 0
        parts.append(f"\u2193 {total_input:,} ({cache_pct}% cached)")
    elif cache_create > 0:
        parts.append(f"\u2193 {total_input:,} (caching)")
    else:
        parts.append(f"\u2193 {total_input:,}")

    parts.append(f"\u2191 {output_tokens:,}")
    parts.append(f"\u03a3 {total:,}")

    if elapsed:
        parts.append(f"\u23f1 {elapsed}")

    if cost:
        parts.append(cost.format())

    line = " \u00b7 ".join(parts)
    console.print(f"{prefix}[token.label]{line}[/]")


def print_inline_status(status_text: str) -> None:
    """Print a dim inline status line (fallback for when no prompt_toolkit toolbar exists)."""
    console = get_console()
    console.print(f"[dim]{status_text}[/]")


# ============================================================================
# Code Change Rendering (Claude-style inline diffs)
# ============================================================================


def print_code_change(change: CodeChange, depth: int = 0) -> None:
    """Print a Claude-style inline diff for edit_file operations.

    Renders:
        \u25b8 Update(file.py)
          \u23bf  Added 1 line, removed 1 line
              1  [project]
              2  name = "foo"
              3 -description = "old"
              3 +description = "new"
              4  license = "MIT"
    """
    console = get_console()
    prefix = _depth_prefix(depth)

    # Header: \u25b8 Update(filename)
    console.print(
        f"{prefix}[tool.bullet]{BULLET_TRIANGLE}[/] [tool.header]Update({change.display_path})[/]"
    )

    # Summary: \u23bf  Added N lines, removed M lines
    console.print(
        f"{prefix}  [dim]\u23bf  {change.summary}[/]"
    )

    if not change.diff_lines:
        return

    # Calculate line number width for alignment
    max_num = max(
        (dl.number for dl in change.diff_lines if dl.number > 0), default=1
    )
    num_width = len(str(max_num))

    for dl in change.diff_lines:
        if dl.marker == "~":
            # Skip/ellipsis line
            pad = " " * (num_width + 1)
            console.print(f"{prefix}  [dim]  {pad}{dl.text}[/]")
        elif dl.marker == "-":
            num_str = str(dl.number).rjust(num_width)
            console.print(
                f"{prefix}  [red]  {num_str} -{dl.text}[/]"
            )
        elif dl.marker == "+":
            num_str = str(dl.number).rjust(num_width)
            console.print(
                f"{prefix}  [green]  {num_str} +{dl.text}[/]"
            )
        else:
            # Context line
            num_str = str(dl.number).rjust(num_width)
            console.print(
                f"{prefix}  [dim]  {num_str}  {dl.text}[/]"
            )


def print_write_summary(file_path: str, line_count: int, depth: int = 0) -> None:
    """Print a summary for write_file operations.

    Renders:
        \u25b8 Write(file.py)
          \u23bf  Created (42 lines)
    """
    console = get_console()
    prefix = _depth_prefix(depth)

    console.print(
        f"{prefix}[tool.bullet]{BULLET_TRIANGLE}[/] [tool.header]Write({file_path})[/]"
    )
    console.print(
        f"{prefix}  [dim]\u23bf  Created ({line_count} lines)[/]"
    )


# ============================================================================
# Helpers
# ============================================================================


def _extract_agent_name(session_id: str) -> str:
    """Extract agent name from session ID."""
    if "_" in session_id:
        parts = session_id.split("_", 1)
        if len(parts) == 2:
            return parts[1]
    return "sub-session"
