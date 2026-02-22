# Streaming UI Redesign — Surgical Implementation Guide

> **For**: Amplifier agent — apply these changes to `amplifier-module-hooks-streaming-ui`
> **Branch**: `feature/session-indicator` (or new branch off it)
> **Dependency**: `rich>=13.0.0` (already satisfied per pyproject.toml)

## Architecture Constraints

- **No Rich Live/Status** — hooks are event-driven, not persistent render loops (see rich_output.py docstring)
- **No ANSI cursor pinning** — conflicts with prompt_toolkit REPL (see terminal.py line 4: "Scroll region / status bar removed - conflicts with prompt_toolkit REPL")
- **Thread-safe** — all output goes through `_output_lock` and a single Console instance
- **stderr** — all UI output goes to stderr, stdout is reserved for program output/pipes
- **Thinking accordion** — Rich has no interactive collapse. Use config-driven expansion levels.

## Change Summary

| # | Change | Files | Risk | Lines |
|---|--------|-------|------|-------|
| 1 | Compact single-line tool ops | `rich_output.py`, `__init__.py`, `state.py` | Low | ~50 |
| 2 | Agent tree with colored borders | `rich_output.py`, `__init__.py`, `state.py` | Low | ~60 |
| 3 | Inline todos under agents | `rich_output.py`, `__init__.py` | Low | ~30 |
| 4 | Thinking block preview/accordion | `rich_output.py`, `__init__.py` | Low | ~40 |
| 5 | Activity spinner | New `spinner.py`, `__init__.py` | Med | ~80 |
| 6 | Status bar data export | New `status_bar.py`, `__init__.py` | Med | ~60 |

---

## 1. `state.py` — Add Fields

### 1a. Add new fields to `SessionState`

Find this block:

```python
@dataclass
class SessionState:
    """State for a single session (root or nested)."""

    session_id: str
    phase: Phase = Phase.IDLE
    start_time: datetime = field(default_factory=datetime.now)

    # Current activity
    current_tool: Optional[ToolCall] = None
    thinking_start: Optional[datetime] = None

    # Metrics
    metrics: SessionMetrics = field(default_factory=SessionMetrics)

    # Nesting
    depth: int = 0
    parent_id: Optional[str] = None

    # Model info for cost calculation
    model: Optional[str] = None
    provider: Optional[str] = None
```

Replace with:

```python
@dataclass
class SessionState:
    """State for a single session (root or nested)."""

    session_id: str
    phase: Phase = Phase.IDLE
    start_time: datetime = field(default_factory=datetime.now)

    # Current activity
    current_tool: Optional[ToolCall] = None
    thinking_start: Optional[datetime] = None

    # Metrics
    metrics: SessionMetrics = field(default_factory=SessionMetrics)

    # Nesting
    depth: int = 0
    parent_id: Optional[str] = None

    # Model info for cost calculation
    model: Optional[str] = None
    provider: Optional[str] = None

    # Agent identity (for sub-sessions)
    agent_name: Optional[str] = None
    agent_type: Optional[str] = None
    agent_desc: Optional[str] = None

    # Buffered tool header for compact single-line output
    pending_tool_header: Optional[str] = None

    # Thinking text accumulator (for accordion preview)
    thinking_text: str = ""
```

### 1b. Add helper to `StateManager` for building agent breadcrumb

Add this method to the `StateManager` class, after `transition()`:

```python
    def get_breadcrumb(self, session_id: str) -> str:
        """Build an agent breadcrumb path like 'main → Explorer → Deep-Scan'."""
        parts: list[str] = []
        current = self.sessions.get(session_id)
        while current:
            name = current.agent_name or ("main" if current.depth == 0 else "sub-session")
            parts.append(name)
            current = self.sessions.get(current.parent_id) if current.parent_id else None
        parts.reverse()
        return " → ".join(parts)
```

---

## 2. `rich_output.py` — Rendering Changes

### 2a. Add depth-color constants and prefix helper

After the existing box-drawing constants block (after line ~115), add:

```python
# Depth-based colors for agent tree
DEPTH_COLORS = ["cyan", "magenta", "green", "yellow", "blue"]


def _depth_prefix(depth: int) -> str:
    """Build a colored tree-branch prefix for the given nesting depth.

    depth=0 → ""
    depth=1 → "│ "  (cyan)
    depth=2 → "│ │ "  (cyan, magenta)
    """
    if depth == 0:
        return ""
    parts = []
    for d in range(1, depth + 1):
        color = DEPTH_COLORS[(d - 1) % len(DEPTH_COLORS)]
        parts.append(f"[{color}]{BOX_VERTICAL}[/] ")
    return "".join(parts)
```

### 2b. Replace `print_session_header` — agent tree headers

Replace the entire function:

```python
def print_session_header(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> None:
    """Print session header — agent tree style for nested sessions."""
    console = get_console()

    if state.depth > 0:
        prefix = _depth_prefix(state.depth)
        color = DEPTH_COLORS[(state.depth - 1) % len(DEPTH_COLORS)]
        agent_name = state.agent_name or _extract_agent_name(state.session_id)
        type_part = f" [dim]({state.agent_type})[/]" if state.agent_type else ""
        desc_part = f" [dim]— {state.agent_desc}[/]" if state.agent_desc else ""
        console.print(
            f"\n{prefix}[{color} bold]{BULLET_TRIANGLE} {agent_name}[/]{type_part}{desc_part}"
        )
    else:
        console.print()
```

### 2c. Replace `print_session_footer` — agent completion summary

Replace the entire function:

```python
def print_session_footer(
    state: SessionState, cost: Optional[CostEstimate] = None
) -> None:
    """Print session footer — compact summary line for nested sessions."""
    if state.depth == 0:
        return

    console = get_console()
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
```

### 2d. Add `print_tool_merged` — compact single-line tool output

Add this new function in the Tool Rendering section (after `print_tool_result`):

```python
def print_tool_merged(
    header: str,
    tool_name: str,
    result: Any,
    success: bool = True,
    depth: int = 0,
    max_lines: int = 10,
) -> None:
    """Print a single merged line: ▸ Header (result).

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

    result_part = f" [{style}]{summary}[/]" if summary else ""
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
```

### 2e. Update `print_tool_call` — use depth prefix

Replace the function body:

```python
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
```

### 2f. Update `print_tool_result` — use depth prefix

Replace the function body:

```python
def print_tool_result(
    tool_name: str,
    result: Any,
    success: bool = True,
    depth: int = 0,
    max_lines: int = 10,
) -> None:
    """Print a tool result — used when header was already printed (slow tools)."""
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
```

### 2g. Update `print_thinking_start` — use depth prefix + spinner char

Replace the function:

```python
def print_thinking_start(depth: int = 0) -> None:
    """Print compact thinking indicator when reasoning begins."""
    console = get_console()
    prefix = _depth_prefix(depth)
    console.print(f"{prefix}[thinking.border]\u2847 Thinking...[/]")
```

Note: `\u2847` is a braille dot pattern used as a static spinner char. The real animated spinner is handled by `spinner.py` (see section 5).

### 2h. Replace `print_thinking_block` — accordion preview

Replace the function:

```python
def print_thinking_block(
    text: str, depth: int = 0, max_preview_lines: int = 3
) -> None:
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
        console.print(
            f"{prefix}  [thinking.elapsed]... +{remaining} lines[/]"
        )
```

### 2i. Update `print_thinking_elapsed` — use depth prefix, richer format

Replace:

```python
def print_thinking_elapsed(seconds: float, depth: int = 0) -> None:
    """Print elapsed thinking time with line count."""
    console = get_console()
    prefix = _depth_prefix(depth)

    if seconds < 60:
        time_str = f"{int(seconds)}s"
    else:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        time_str = f"{minutes}m {secs}s"

    console.print(f"{prefix}[thinking.elapsed]{CHECK} Reasoned for {time_str}[/]")
```

### 2j. Update `print_token_usage` — use depth prefix

In the existing function, replace `indent = "  " * depth` with `prefix = _depth_prefix(depth)` and update the final console.print:

```python
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
```

### 2k. Update `print_task_checklist` — use depth prefix

Replace:

```python
def print_task_checklist(
    items: list[tuple[str, str]],
    depth: int = 0,
) -> None:
    """Print a task checklist with three states — inline style (no box)."""
    console = get_console()
    prefix = _depth_prefix(depth)

    for status, label in items:
        if status == "done":
            console.print(f"{prefix}  [green]{TASK_DONE}[/] [green]{label}[/]")
        elif status == "active":
            console.print(f"{prefix}  [yellow]{TASK_ACTIVE}[/] {label}")
        else:
            console.print(f"{prefix}  [dim]{TASK_PENDING} {label}[/]")
```

### 2l. Add new import for `print_tool_merged`

In the import block at the top of `rich_output.py`, no new external imports are needed. The function uses existing imports.

**But** — update `__init__.py`'s import block to include the new function:

```python
from .rich_output import (
    print_session_footer,
    print_session_header,
    print_thinking_block,
    print_thinking_elapsed,
    print_thinking_start,
    print_token_usage,
    print_tool_call,
    print_tool_merged,  # NEW
    print_tool_result,
)
```

---

## 3. `__init__.py` — Hook Handler Changes

### 3a. Compact tool lines — buffer fast tools, print slow tools immediately

Replace `handle_tool_pre`:

```python
    # Fast tools — buffer header, merge with result in tool:post
    SLOW_TOOLS = {"bash", "shell", "delegate"}

    async def handle_tool_pre(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle tool invocation — display smart tool header."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        tool_name = data.get("tool_name", "unknown")
        tool_input = data.get("tool_input", {})

        if state:
            state.phase = Phase.TOOL_RUNNING
            state.current_tool = ToolCall(name=tool_name, arguments=tool_input)
            state.metrics.tool_calls += 1

        depth = state.depth if state else 0

        if tool_name.lower() in self.SLOW_TOOLS:
            # Slow tools: print header immediately so user sees activity
            with self._output_lock:
                print_tool_call(tool_name, tool_input, depth, self._cwd)
            if state:
                state.pending_tool_header = None
        else:
            # Fast tools: buffer header for single-line merge in tool:post
            from .formatting import format_tool_header
            if state:
                state.pending_tool_header = format_tool_header(
                    tool_name, tool_input, self._cwd
                )

        # For delegate tools, extract agent info for the child session
        if tool_name.lower() == "delegate" and state:
            state._pending_agent_info = {
                "agent": tool_input.get("agent", ""),
                "instruction": tool_input.get("instruction", ""),
            }

        return HookResult(action="continue")
```

Replace `handle_tool_post`:

```python
    async def handle_tool_post(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle tool result — display result summary."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        tool_name = data.get("tool_name", "unknown")
        result = data.get("tool_response", data.get("result", {}))
        success = not is_error_result(result)

        if state:
            state.phase = Phase.STREAMING
            state.current_tool = None

        if self.show_tool_output:
            depth = state.depth if state else 0
            with self._output_lock:
                if state and state.pending_tool_header:
                    # Merged single-line output for fast tools
                    print_tool_merged(
                        state.pending_tool_header,
                        tool_name,
                        result,
                        success,
                        depth,
                        self.max_tool_lines,
                    )
                    state.pending_tool_header = None
                else:
                    # Header was already printed (slow tool) — show result only
                    print_tool_result(
                        tool_name, result, success, depth, self.max_tool_lines
                    )

        return HookResult(action="continue")
```

### 3b. Agent tree — pass agent info to child sessions

Replace `handle_task_spawned`:

```python
    async def handle_task_spawned(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle sub-agent task spawn — create child session with agent metadata."""
        child_session_id = data.get("child_session_id")
        parent_session_id = data.get("parent_session_id") or data.get("session_id")
        model = data.get("model")
        provider = data.get("provider")

        if child_session_id:
            state = self.state_manager.get_or_create(
                child_session_id,
                parent_session_id,
                model=model,
                provider=provider,
            )

            # Transfer agent info from parent's pending delegate call
            parent = self.state_manager.get(parent_session_id) if parent_session_id else None
            if parent and hasattr(parent, "_pending_agent_info") and parent._pending_agent_info:
                info = parent._pending_agent_info
                agent_str = info.get("agent", "")
                instruction = info.get("instruction", "")

                # Parse "bundle:agent_type" format
                if ":" in agent_str:
                    parts = agent_str.split(":")
                    state.agent_type = parts[-1]
                    state.agent_name = parts[-1].replace("-", " ").title()
                else:
                    state.agent_type = agent_str
                    state.agent_name = agent_str.replace("-", " ").title() if agent_str else None

                # Short description from instruction
                if instruction:
                    state.agent_desc = (
                        instruction[:60] + "..." if len(instruction) > 60 else instruction
                    )

                parent._pending_agent_info = None

            with self._output_lock:
                print_session_header(state)

        return HookResult(action="continue")
```

### 3c. Thinking accordion — accumulate text, show preview on end

In `handle_content_block_start`, the existing code is fine. No changes needed.

In `handle_content_block_end`, update the thinking block completion section. Find:

```python
        # Handle thinking block completion
        if (
            block_type in {"thinking", "reasoning"}
            and block_index is not None
            and block_index in self.thinking_blocks
        ):
            thinking_info = self.thinking_blocks[block_index]

            # Show elapsed thinking time (compact - no full text)
            if self.show_thinking and thinking_info.get("start_time"):
                elapsed = (
                    datetime.now() - thinking_info["start_time"]
                ).total_seconds()
                if elapsed > 1:
                    with self._output_lock:
                        print_thinking_elapsed(elapsed, depth)

            del self.thinking_blocks[block_index]

            if state:
                self.state_manager.transition(session_id, Phase.STREAMING)
```

Replace with:

```python
        # Handle thinking block completion
        if (
            block_type in {"thinking", "reasoning"}
            and block_index is not None
            and block_index in self.thinking_blocks
        ):
            thinking_info = self.thinking_blocks[block_index]

            # Extract thinking text for accordion preview
            thinking_text = ""
            if block_type == "thinking":
                thinking_text = block.get("thinking", "")
            elif block_type == "reasoning":
                # Reasoning blocks may have summary or content lists
                summary = block.get("summary", [])
                content = block.get("content", [])
                parts = []
                for item in (summary if isinstance(summary, list) else [summary]):
                    if isinstance(item, dict):
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                for item in (content if isinstance(content, list) else [content]):
                    if isinstance(item, dict):
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                thinking_text = "\n".join(p for p in parts if p)

            # Store thinking text on state (for CLI access / replay)
            if state and thinking_text:
                state.thinking_text += thinking_text + "\n"

            # Show elapsed time + optional preview
            if self.show_thinking and thinking_info.get("start_time"):
                elapsed = (
                    datetime.now() - thinking_info["start_time"]
                ).total_seconds()
                if elapsed > 1:
                    with self._output_lock:
                        print_thinking_elapsed(elapsed, depth)
                        # Show preview if configured (0 = no preview, default)
                        if self.thinking_preview_lines > 0 and thinking_text:
                            print_thinking_block(
                                thinking_text, depth, self.thinking_preview_lines
                            )

            del self.thinking_blocks[block_index]

            if state:
                self.state_manager.transition(session_id, Phase.STREAMING)
```

### 3d. Add `thinking_preview_lines` config param

In `__init__`, add the new parameter. Find:

```python
    def __init__(
        self,
        show_thinking: bool = True,
        show_tool_output: bool = True,
        max_tool_lines: int = 10,
        show_token_usage: bool = True,
    ):
        self.show_thinking = show_thinking
        self.show_tool_output = show_tool_output
        self.max_tool_lines = max_tool_lines
        self.show_token_usage = show_token_usage
```

Replace with:

```python
    def __init__(
        self,
        show_thinking: bool = True,
        show_tool_output: bool = True,
        max_tool_lines: int = 10,
        show_token_usage: bool = True,
        show_status_bar: bool = True,
        thinking_preview_lines: int = 0,
    ):
        self.show_thinking = show_thinking
        self.show_tool_output = show_tool_output
        self.max_tool_lines = max_tool_lines
        self.show_token_usage = show_token_usage
        self.show_status_bar = show_status_bar
        self.thinking_preview_lines = thinking_preview_lines
```

And update `mount()` to pass the new config:

```python
async def mount(coordinator: Any, config: dict[str, Any]) -> None:
    """Mount streaming UI hooks module."""
    ui_config = config.get("ui", {})

    hooks = StreamingUIHooks(
        show_thinking=ui_config.get("show_thinking", True),
        show_tool_output=ui_config.get("show_tool_output", True),
        max_tool_lines=ui_config.get("max_tool_lines", 10),
        show_token_usage=ui_config.get("show_token_usage", True),
        show_status_bar=ui_config.get("show_status_bar", True),
        thinking_preview_lines=ui_config.get("thinking_preview_lines", 0),
    )
```

### 3e. Fix thinking_blocks key collision (existing bug)

In `__init__` of `StreamingUIHooks`, update the type annotation:

```python
        # Thinking block tracking — keyed by (session_id, block_index)
        self.thinking_blocks: dict[tuple[str, int], dict[str, Any]] = {}
```

In `handle_content_block_start`, the `thinking_blocks` dict is keyed by bare `block_index`, which collides across concurrent sessions. Change the key to `(session_id, block_index)`.

Find in `handle_content_block_start`:

```python
            if block_index is not None:
                self.thinking_blocks[block_index] = {
```

Replace with:

```python
            if block_index is not None:
                key = (session_id, block_index)
                self.thinking_blocks[key] = {
```

And in `handle_content_block_end`, change the lookup. Find:

```python
            and block_index in self.thinking_blocks
        ):
            thinking_info = self.thinking_blocks[block_index]
```

Replace with:

```python
        key = (session_id, block_index) if block_index is not None else None
        if (
            block_type in {"thinking", "reasoning"}
            and key is not None
            and key in self.thinking_blocks
        ):
            thinking_info = self.thinking_blocks[key]
```

And the cleanup line. Find:

```python
            del self.thinking_blocks[block_index]
```

Replace with:

```python
            del self.thinking_blocks[key]
```

---

## 4. New File: `spinner.py`

Create `amplifier_module_hooks_streaming_ui/spinner.py`:

```python
"""Lightweight activity spinner for the streaming UI.

Provides a non-blocking animated spinner using carriage return (\r)
to overwrite the current line on stderr. Does NOT use Rich Live/Status
(which require a persistent render loop incompatible with hooks).

Thread-safe: uses a daemon timer thread that auto-stops when main exits.
"""

import sys
import threading
from typing import Optional

from .rich_output import get_console, _depth_prefix

# Braille spinner frames (smooth 10-frame animation)
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPINNER_INTERVAL = 0.1  # seconds between frames


class Spinner:
    """Animated spinner that overwrites the current terminal line.

    Usage:
        spinner = Spinner("Thinking...", depth=1)
        spinner.start()
        # ... long operation ...
        spinner.stop()  # clears the spinner line
    """

    def __init__(self, message: str = "Thinking...", depth: int = 0):
        self.message = message
        self.depth = depth
        self._frame_idx = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._running = False
        self._file = sys.stderr

    def start(self) -> None:
        """Start the spinner animation."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._frame_idx = 0
        self._tick()

    def stop(self, clear: bool = True) -> None:
        """Stop the spinner and optionally clear its line."""
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None

        if clear:
            # Clear the spinner line
            try:
                self._file.write("\r\033[K")
                self._file.flush()
            except (OSError, ValueError):
                pass

    def update_message(self, message: str) -> None:
        """Update the spinner message while running."""
        with self._lock:
            self.message = message

    def _tick(self) -> None:
        """Render one spinner frame and schedule the next."""
        with self._lock:
            if not self._running:
                return
            frame = SPINNER_FRAMES[self._frame_idx % len(SPINNER_FRAMES)]
            self._frame_idx += 1
            msg = self.message

        # Build the line (plain text — Rich markup not used here
        # because we write directly to stderr with \r)
        prefix_plain = "│ " * self.depth  # simplified plain-text prefix
        line = f"\r{prefix_plain}{frame} {msg}"

        try:
            self._file.write(line)
            self._file.flush()
        except (OSError, ValueError):
            return

        with self._lock:
            if self._running:
                self._timer = threading.Timer(SPINNER_INTERVAL, self._tick)
                self._timer.daemon = True
                self._timer.start()


class SpinnerManager:
    """Manages spinner lifecycle for the streaming UI hooks.

    Ensures only one spinner is active at a time.
    """

    def __init__(self):
        self._current: Optional[Spinner] = None
        self._lock = threading.Lock()

    def start(self, message: str = "Thinking...", depth: int = 0) -> None:
        """Start a spinner (stops any existing one first)."""
        with self._lock:
            if self._current:
                self._current.stop(clear=True)
            self._current = Spinner(message, depth)
            self._current.start()

    def stop(self) -> None:
        """Stop the current spinner."""
        with self._lock:
            if self._current:
                self._current.stop(clear=True)
                self._current = None

    def update(self, message: str) -> None:
        """Update the current spinner's message."""
        with self._lock:
            if self._current:
                self._current.update_message(message)

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._current is not None and self._current._running
```

### Wire spinner into `__init__.py`

Add import at top of `__init__.py`:

```python
from .spinner import SpinnerManager
```

In `__init__` of `StreamingUIHooks`, add:

```python
        # Activity spinner
        self._spinner = SpinnerManager() if show_status_bar else None
```

In `handle_content_block_start`, after printing the thinking indicator, start spinner:

```python
                with self._output_lock:
                    print_thinking_start(depth)
                # Start animated spinner
                if self._spinner:
                    self._spinner.start("Thinking...", depth)
```

In `handle_content_block_end`, before printing elapsed time, stop spinner:

```python
            # Stop spinner before printing
            if self._spinner:
                self._spinner.stop()
```

In `handle_tool_pre`, for slow tools, update spinner:

```python
        if tool_name.lower() in self.SLOW_TOOLS:
            with self._output_lock:
                print_tool_call(tool_name, tool_input, depth, self._cwd)
            if self._spinner:
                from .formatting import format_tool_header
                self._spinner.start(
                    format_tool_header(tool_name, tool_input, self._cwd), depth
                )
```

In `handle_tool_post`, stop spinner:

```python
        # Stop any active spinner
        if self._spinner:
            self._spinner.stop()
```

---

## 5. New File: `status_bar.py`

Create `amplifier_module_hooks_streaming_ui/status_bar.py`:

> **Important**: This module exports status DATA. It does NOT render to the terminal.
> The Amplifier CLI (prompt_toolkit) should call `get_status()` to render in its
> bottom toolbar. Direct ANSI cursor pinning was removed because it conflicts
> with prompt_toolkit (see terminal.py line 4).

```python
"""Status bar data provider for the streaming UI.

Exports a thread-safe StatusInfo dataclass that the CLI's prompt_toolkit
bottom_toolbar can read and render. The hooks module updates this on every
event. The CLI polls or subscribes to changes.

Architecture:
    hooks ──update_status()──> StatusInfo (thread-safe)
    CLI   ──get_status()────> reads StatusInfo for toolbar rendering
"""

import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StatusInfo:
    """Current status for the footer bar."""

    # Phase display
    phase: str = "Ready"
    phase_style: str = "green"  # green=ready, yellow=thinking, cyan=tool, red=error

    # Context breadcrumb (e.g. "main → Explorer → Deep-Scan")
    breadcrumb: str = ""

    # Current tool (when running)
    current_tool: str = ""

    # Token counts
    input_tokens: int = 0
    output_tokens: int = 0
    cache_pct: int = 0

    # Timing
    elapsed: str = ""

    # Model info
    model: str = ""

    # Error (most recent)
    last_error: str = ""

    # Cost
    cost: str = ""


class StatusBarProvider:
    """Thread-safe status info provider.

    The hooks module calls update() on every event.
    The CLI calls get_status() to read current state.
    """

    def __init__(self):
        self._status = StatusInfo()
        self._lock = threading.Lock()
        self._on_change: Optional[callable] = None

    def get_status(self) -> StatusInfo:
        """Get current status (thread-safe read)."""
        with self._lock:
            # Return a copy to avoid race conditions
            return StatusInfo(
                phase=self._status.phase,
                phase_style=self._status.phase_style,
                breadcrumb=self._status.breadcrumb,
                current_tool=self._status.current_tool,
                input_tokens=self._status.input_tokens,
                output_tokens=self._status.output_tokens,
                cache_pct=self._status.cache_pct,
                elapsed=self._status.elapsed,
                model=self._status.model,
                last_error=self._status.last_error,
                cost=self._status.cost,
            )

    def update(self, **kwargs) -> None:
        """Update status fields (thread-safe write)."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._status, key):
                    setattr(self._status, key, value)

        # Notify listener if registered
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def on_change(self, callback: callable) -> None:
        """Register a callback for status changes (e.g. to invalidate prompt_toolkit)."""
        self._on_change = callback

    def format_toolbar(self) -> str:
        """Format status as a plain string for prompt_toolkit bottom_toolbar.

        Returns a string like:
        ● Thinking │ main → Explorer │ ↓ 87k · ↑ 625 │ ⏱ 00:28 │ opus-4-6
        """
        s = self.get_status()
        parts = []

        # Phase indicator
        if s.phase == "Ready":
            parts.append("● Ready")
        elif s.phase == "Error":
            parts.append(f"✗ {s.last_error}" if s.last_error else "✗ Error")
        elif s.current_tool:
            parts.append(f"⠋ {s.current_tool}")
        else:
            parts.append(f"⠋ {s.phase}")

        # Breadcrumb
        if s.breadcrumb:
            parts.append(s.breadcrumb)

        # Tokens (compact)
        if s.input_tokens > 0:
            inp = _compact_number(s.input_tokens)
            out = _compact_number(s.output_tokens)
            token_str = f"↓ {inp}"
            if s.cache_pct > 0:
                token_str += f" ({s.cache_pct}%)"
            token_str += f" · ↑ {out}"
            parts.append(token_str)

        # Elapsed
        if s.elapsed:
            parts.append(f"⏱ {s.elapsed}")

        # Cost
        if s.cost:
            parts.append(s.cost)

        # Model
        if s.model:
            # Shorten model name
            short = s.model.replace("claude-", "").replace("-20250514", "")
            parts.append(short)

        return " │ ".join(parts)


def _compact_number(n: int) -> str:
    """Format large numbers compactly: 1234 -> '1.2k', 1234567 -> '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
```

### Wire status bar into `__init__.py`

Add import:

```python
from .status_bar import StatusBarProvider
```

In `__init__` of `StreamingUIHooks`:

```python
        # Status bar provider (CLI reads this for prompt_toolkit toolbar)
        self.status_bar = StatusBarProvider() if show_status_bar else None
```

Then add a helper method to update status after every hook:

```python
    def _update_status(self, session_id: str) -> None:
        """Push current state to the status bar provider."""
        if not self.status_bar:
            return

        state = self.state_manager.get(session_id)
        if not state:
            return

        from .state import PHASE_DISPLAY

        phase_styles = {
            Phase.IDLE: ("Ready", "green"),
            Phase.THINKING: ("Thinking", "yellow"),
            Phase.STREAMING: ("Responding", "green"),
            Phase.TOOL_CALLING: ("Calling tool", "cyan"),
            Phase.TOOL_RUNNING: ("Running", "cyan"),
            Phase.COMPLETE: ("Done", "green"),
            Phase.ERROR: ("Error", "red"),
        }

        phase_text, phase_style = phase_styles.get(state.phase, ("Unknown", "dim"))

        root = self.state_manager.get_root()
        elapsed = root.elapsed_formatted() if root else state.elapsed_formatted()

        total_input = state.metrics.input_tokens + state.metrics.cache_read_tokens
        cache_pct = (
            int((state.metrics.cache_read_tokens / total_input) * 100)
            if total_input > 0
            else 0
        )

        self.status_bar.update(
            phase=phase_text,
            phase_style=phase_style,
            breadcrumb=self.state_manager.get_breadcrumb(session_id),
            current_tool=(
                state.current_tool.name if state.current_tool else ""
            ),
            input_tokens=total_input,
            output_tokens=state.metrics.output_tokens,
            cache_pct=cache_pct,
            elapsed=elapsed,
            model=state.model or "",
        )
```

Call `self._update_status(session_id)` at the END of every handler method (handle_session_start, handle_tool_pre, handle_tool_post, handle_content_block_start, handle_content_block_end, handle_task_spawned, handle_task_complete).

---

## 6. Update `__all__` and Exports

In `__init__.py`, update `__all__`:

```python
__all__ = ["mount", "StreamingUIHooks", "StateManager", "StatusBarProvider"]
```

---

## 7. CLI Integration Guide (for Amplifier CLI team)

The status bar data is now available via `hooks.status_bar.get_status()`. To render it in the prompt_toolkit bottom toolbar:

```python
# In the Amplifier CLI REPL setup (prompt_toolkit)
from prompt_toolkit.formatted_text import HTML

def bottom_toolbar():
    if streaming_ui_hooks and streaming_ui_hooks.status_bar:
        text = streaming_ui_hooks.status_bar.format_toolbar()
        return HTML(f'<style bg="#16162a" fg="#888888"> {text} </style>')
    return ""

# Pass to PromptSession
session = PromptSession(bottom_toolbar=bottom_toolbar)

# To force toolbar refresh on status changes:
streaming_ui_hooks.status_bar.on_change(
    lambda: app.invalidate()  # prompt_toolkit app invalidation
)
```

---

## 8. Test Updates Required

### Broken tests to fix

1. **`test_thinking_block_end`** (line 138): Asserts `"Thinking:" in output` and `"This is a test thought process." in output` — but `print_thinking_block` was already a no-op before this change. These assertions are stale. Update to check for the elapsed time output instead, or set `thinking_preview_lines=3` in the test hooks to test the accordion.

2. **`test_reasoning_block_end`** (line 159): Same issue — asserts text content that compact mode doesn't render.

3. **`test_tool_pre_displays_smart_header`** (line 206): With the buffering change, `handle_tool_pre` for `read_file` (a fast tool) no longer prints to console. The test should instead call `handle_tool_post` after `handle_tool_pre` and check the merged output.

4. **`test_mount_registers_hooks`** (line 66): Works as-is since mount() still registers the same 8 events.

5. **`_make_hooks`** helper (line 27): Already passes `show_status_bar=False` — good, this prevents spinner/status bar threads in tests.

### New tests to add

- Test merged single-line output for fast tools (read_file pre+post → one line)
- Test slow tool output is still two lines (bash pre → header, post → result)
- Test agent tree headers appear with correct agent name
- Test thinking_blocks keyed by (session_id, block_index)
- Test spinner doesn't start when show_status_bar=False

---

## 9. `renderer.py` Compatibility Shim

The existing `renderer.py` is a compatibility shim that proxies to `rich_output.py`. No changes are required because all modified functions retain backward-compatible signatures (new params have defaults). However, if any external code calls `render_tool_call()` directly (bypassing hooks), it won't get the compact single-line behavior — it will still use the two-line format via `print_tool_call`. This is acceptable for a compatibility path.

If you want the shim to also support merged output, add:

```python
def render_tool_merged(
    header: str, tool_name: str, result: Any, success: bool = True,
    depth: int = 0, max_lines: int = MAX_TOOL_OUTPUT_LINES,
) -> str:
    from .rich_output import print_tool_merged
    print_tool_merged(header, tool_name, result, success, depth, max_lines)
    return ""
```

---

## 10. Dead Code Cleanup (optional, separate PR)

These can be removed in a follow-up:

1. **`_flatten_content()`** in `__init__.py` (line 362-393) — defined but never called
2. **`terminal.py`** — ANSI constants only used by archived code; the module can be deleted if `renderer.py` shim and `archive/` are also removed
3. **`archive/` directory** — superseded by Rich rendering
4. **`renderer.py`** — compatibility shim; audit external consumers before removing

---

## Quick Reference: File Changes

```
amplifier_module_hooks_streaming_ui/
├── __init__.py          ← MODIFY (handle_tool_pre/post, handle_task_spawned,
│                           handle_content_block_end, __init__, mount)
├── state.py             ← MODIFY (add fields to SessionState, add get_breadcrumb)
├── rich_output.py       ← MODIFY (all print_* functions updated for depth prefix,
│                           add print_tool_merged, update session header/footer)
├── formatting.py        ← NO CHANGES
├── cost.py              ← NO CHANGES
├── spinner.py           ← NEW FILE
├── status_bar.py        ← NEW FILE
├── terminal.py          ← NO CHANGES (future: delete)
└── renderer.py          ← NO CHANGES (future: delete)
```
