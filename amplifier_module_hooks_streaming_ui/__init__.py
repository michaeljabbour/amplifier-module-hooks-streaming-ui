"""Streaming UI Hooks Module

Display streaming LLM output (thinking blocks, tool calls, and token usage) to console.
Includes session activity indicator with spinner, elapsed time, and stuck detection.
"""

# Amplifier module metadata
__amplifier_module_type__ = "hook"

import logging
import sys
import threading
import time
from datetime import datetime
from typing import Any

from amplifier_core.models import HookResult
from rich.console import Console
from rich.markdown import Markdown

logger = logging.getLogger(__name__)

# Spinner frames (same style as make/cargo)
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


async def mount(coordinator: Any, config: dict[str, Any]) -> None:
    """Mount streaming UI hooks module.

    Args:
        coordinator: The amplifier coordinator instance
        config: Configuration from profile
    """
    # Extract config from ui section
    ui_config = config.get("ui", {})
    show_thinking = ui_config.get("show_thinking_stream", True)
    show_tool_lines = ui_config.get("show_tool_lines", 5)
    show_token_usage = ui_config.get("show_token_usage", True)
    
    # Session indicator config
    show_elapsed = ui_config.get("show_elapsed", True)
    stuck_threshold = ui_config.get("stuck_threshold", 60.0)
    spinner_interval = ui_config.get("spinner_interval", 0.1)

    # Create hook handlers
    hooks = StreamingUIHooks(
        show_thinking, 
        show_tool_lines, 
        show_token_usage,
        show_elapsed=show_elapsed,
        stuck_threshold=stuck_threshold,
        spinner_interval=spinner_interval,
    )

    # Register hooks on the coordinator
    coordinator.hooks.register("session:start", hooks.handle_session_start)
    coordinator.hooks.register("session:end", hooks.handle_session_end)
    coordinator.hooks.register("content_block:start", hooks.handle_content_block_start)
    coordinator.hooks.register("content_block:end", hooks.handle_content_block_end)
    coordinator.hooks.register("tool:pre", hooks.handle_tool_pre)
    coordinator.hooks.register("tool:post", hooks.handle_tool_post)

    # Log successful mount
    logger.info("Mounted hooks-streaming-ui")

    return


class StreamingUIHooks:
    """Hooks for displaying streaming UI output with session activity indicator."""

    def __init__(
        self, 
        show_thinking: bool, 
        show_tool_lines: int, 
        show_token_usage: bool,
        show_elapsed: bool = True,
        stuck_threshold: float = 60.0,
        spinner_interval: float = 0.1,
    ):
        """Initialize streaming UI hooks.

        Args:
            show_thinking: Whether to display thinking blocks
            show_tool_lines: Number of lines to show for tool I/O
            show_token_usage: Whether to display token usage
            show_elapsed: Whether to show elapsed session time
            stuck_threshold: Seconds of inactivity before showing warning
            spinner_interval: Seconds between spinner frame updates
        """
        self.show_thinking = show_thinking
        self.show_tool_lines = show_tool_lines
        self.show_token_usage = show_token_usage
        self.show_elapsed = show_elapsed
        self.stuck_threshold = stuck_threshold
        self.spinner_interval = spinner_interval
        
        self.thinking_blocks: dict[int, dict[str, Any]] = {}
        
        # Session state tracking
        self.session_start: datetime | None = None
        self.last_activity: datetime | None = None
        self.current_state: str = "idle"  # idle, thinking, tool
        self.current_tool: str | None = None
        self.spinner_frame: int = 0
        
        # Spinner thread control
        self._spinner_running = False
        self._spinner_thread: threading.Thread | None = None
        self._spinner_lock = threading.Lock()
        self._last_spinner_line: str = ""

    def _format_elapsed(self) -> str:
        """Format elapsed time since session start."""
        if not self.session_start:
            return ""
        elapsed = datetime.now() - self.session_start
        total_seconds = int(elapsed.total_seconds())
        minutes, seconds = divmod(total_seconds, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _get_spinner_frame(self) -> str:
        """Get the next spinner frame."""
        frame = SPINNER_FRAMES[self.spinner_frame % len(SPINNER_FRAMES)]
        self.spinner_frame += 1
        return frame

    def _check_stuck(self) -> str:
        """Check if session appears stuck and return warning if so."""
        if not self.last_activity:
            return ""
        idle_seconds = (datetime.now() - self.last_activity).total_seconds()
        if idle_seconds > self.stuck_threshold:
            return f" \033[33m⚠ {int(idle_seconds)}s idle\033[0m"
        return ""

    def _update_spinner_line(self):
        """Update the spinner status line (called from background thread)."""
        with self._spinner_lock:
            if not self._spinner_running:
                return
            
            frame = self._get_spinner_frame()
            
            # Build status parts
            parts = []
            
            # State indicator
            if self.current_state == "thinking":
                parts.append(f"{frame} Thinking")
            elif self.current_state == "tool":
                tool_name = self.current_tool or "tool"
                parts.append(f"{frame} {tool_name}")
            else:
                parts.append(f"{frame} Processing")
            
            # Elapsed time
            if self.show_elapsed:
                elapsed = self._format_elapsed()
                if elapsed:
                    parts.append(f"[{elapsed}]")
            
            # Stuck warning
            stuck = self._check_stuck()
            
            line = " ".join(parts) + stuck
            
            # Use \r to overwrite the line
            # Clear previous line content first (handle varying lengths)
            clear_len = max(len(self._last_spinner_line), len(line)) + 5
            sys.stderr.write(f"\r{' ' * clear_len}\r\033[36m{line}\033[0m")
            sys.stderr.flush()
            self._last_spinner_line = line

    def _spinner_loop(self):
        """Background thread that updates the spinner."""
        while self._spinner_running:
            self._update_spinner_line()
            time.sleep(self.spinner_interval)

    def _start_spinner(self, state: str, tool_name: str | None = None):
        """Start the background spinner with given state."""
        with self._spinner_lock:
            self.current_state = state
            self.current_tool = tool_name
            self.last_activity = datetime.now()
            
            if not self._spinner_running:
                self._spinner_running = True
                self._spinner_thread = threading.Thread(target=self._spinner_loop, daemon=True)
                self._spinner_thread.start()

    def _stop_spinner(self, clear: bool = True):
        """Stop the spinner and optionally clear the line."""
        with self._spinner_lock:
            self._spinner_running = False
            self.current_state = "idle"
            self.current_tool = None
            
            if clear and self._last_spinner_line:
                # Clear the spinner line
                clear_len = len(self._last_spinner_line) + 10
                sys.stderr.write(f"\r{' ' * clear_len}\r")
                sys.stderr.flush()
                self._last_spinner_line = ""

    def _parse_agent_from_session_id(self, session_id: str | None) -> str | None:
        """Extract agent name from hierarchical session ID."""
        if not session_id:
            return None
        if "_" in session_id:
            parts = session_id.split("_", 1)
            if len(parts) == 2:
                return parts[1]
        return None

    async def handle_session_start(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle session start - initialize timing."""
        self.session_start = datetime.now()
        self.last_activity = datetime.now()
        return HookResult(action="continue")

    async def handle_session_end(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle session end - stop spinner."""
        self._stop_spinner(clear=True)
        return HookResult(action="continue")

    async def handle_content_block_start(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Detect thinking blocks and prepare for display."""
        block_type = data.get("block_type")
        block_index = data.get("block_index")
        session_id = data.get("session_id")
        agent_name = self._parse_agent_from_session_id(session_id)

        if (
            block_type in {"thinking", "reasoning"}
            and self.show_thinking
            and block_index is not None
        ):
            self.thinking_blocks[block_index] = {"started": True, "agent": agent_name}
            
            # Start spinner for thinking
            self._start_spinner("thinking")
            
            if agent_name:
                sys.stderr.write(
                    f"\n    \033[36m🤔 [{agent_name}] Thinking...\033[0m\n"
                )
                sys.stderr.flush()
            else:
                sys.stderr.write("\n\033[36m🧠 Thinking...\033[0m\n")
                sys.stderr.flush()

        return HookResult(action="continue")

    async def handle_content_block_end(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Display complete thinking block and token usage."""
        block_index = data.get("block_index")
        total_blocks = data.get("total_blocks")
        block = data.get("block", {})
        block_type = block.get("type")
        usage = data.get("usage")
        is_last_block = block_index == total_blocks - 1 if total_blocks else False

        session_id = data.get("session_id")
        agent_name = self._parse_agent_from_session_id(session_id)

        if block_index in self.thinking_blocks:
            tracked_agent = self.thinking_blocks[block_index].get("agent")
            if tracked_agent:
                agent_name = tracked_agent

        if (
            block_type in {"thinking", "reasoning"}
            and block_index is not None
            and block_index in self.thinking_blocks
        ):
            self._stop_spinner(clear=True)
            
            thinking_text = (
                block.get("thinking", "")
                or block.get("text", "")
                or _flatten_reasoning_block(block)
            )

            if thinking_text:
                if agent_name:
                    print(f"\n    \033[90m{'=' * 56}\033[0m")
                    print(f"    \033[90m[{agent_name}] Thinking:\033[0m")
                    print(f"    \033[90m{'-' * 56}\033[0m")
                    from io import StringIO
                    buffer = StringIO()
                    temp_console = Console(file=buffer, highlight=False, width=52)
                    temp_console.print(Markdown(thinking_text))
                    rendered = buffer.getvalue()
                    for line in rendered.rstrip().split("\n"):
                        print(f"    \033[2m{line}\033[0m")
                    print(f"    \033[90m{'=' * 56}\033[0m\n")
                else:
                    from io import StringIO
                    buffer = StringIO()
                    temp_console = Console(file=buffer, highlight=False, width=60)
                    temp_console.print(Markdown(thinking_text))
                    rendered = buffer.getvalue()
                    print(f"\n\033[90m{'=' * 60}\033[0m")
                    print("\033[90mThinking:\033[0m")
                    print(f"\033[90m{'-' * 60}\033[0m")
                    print(f"\033[2m{rendered.rstrip()}\033[0m")
                    print(f"\033[90m{'=' * 60}\033[0m\n")

            del self.thinking_blocks[block_index]

        if is_last_block and self.show_token_usage and usage:
            indent = "    " if agent_name else ""
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            cache_create = usage.get("cache_creation_input_tokens", 0)
            total_input = input_tokens + cache_read + cache_create
            total_tokens = total_input + output_tokens

            input_str = f"{total_input:,}"
            output_str = f"{output_tokens:,}"
            total_str = f"{total_tokens:,}"

            cache_info = ""
            if cache_read > 0 or cache_create > 0:
                cache_pct = (
                    int((cache_read / total_input) * 100) if total_input > 0 else 0
                )
                if cache_read > 0:
                    cache_info = f" ({cache_pct}% cached)"
                else:
                    cache_info = " (caching...)"

            # Add elapsed time to token usage line
            elapsed_str = ""
            if self.show_elapsed:
                elapsed = self._format_elapsed()
                if elapsed:
                    elapsed_str = f" | ⏱ {elapsed}"

            print(f"{indent}\033[2m│  📊 Token Usage\033[0m")
            print(
                f"{indent}\033[2m└─ Input: {input_str}{cache_info} | Output: {output_str} | Total: {total_str}{elapsed_str}\033[0m"
            )

        self.last_activity = datetime.now()
        return HookResult(action="continue")

    async def handle_tool_pre(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Display tool invocation with truncated input."""
        tool_name = data.get("tool_name", "unknown")
        tool_input = data.get("tool_input", {})
        session_id = data.get("session_id")

        self._stop_spinner(clear=True)
        self._start_spinner("tool", tool_name)

        agent_name = self._parse_agent_from_session_id(session_id)
        input_str = self._format_for_display(tool_input)
        truncated = self._truncate_lines(input_str, self.show_tool_lines)

        if agent_name:
            print(f"\n    \033[36m┌─ 🔧 [{agent_name}] Using tool: {tool_name}\033[0m")
            for line in truncated.split("\n"):
                print(f"    \033[36m│\033[0m  \033[2m{line}\033[0m")
        else:
            print(f"\n\033[36m🔧 Using tool: {tool_name}\033[0m")
            for line in truncated.split("\n"):
                print(f"   \033[2m{line}\033[0m")

        return HookResult(action="continue")

    async def handle_tool_post(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Display tool result with truncated output."""
        self._stop_spinner(clear=True)
        
        tool_name = data.get("tool_name", "unknown")
        result = data.get("tool_response", data.get("result", {}))
        session_id = data.get("session_id")
        agent_name = self._parse_agent_from_session_id(session_id)

        if isinstance(result, dict):
            raw_output = result.get("output")
            bash_output = raw_output if isinstance(raw_output, dict) else result
            if isinstance(bash_output, dict) and "returncode" in bash_output:
                stdout = bash_output.get("stdout", "")
                stderr = bash_output.get("stderr", "")
                returncode = bash_output.get("returncode", 0)
                success = returncode == 0
                if success:
                    output = stdout or stderr or "(no output)"
                else:
                    output = stdout
                    if stderr:
                        output = (
                            f"{output}\n[stderr]: {stderr}"
                            if output
                            else f"[stderr]: {stderr}"
                        )
                    output = output or "(no output)"
            else:
                success = result.get("success", True)
                output = self._format_for_display(
                    raw_output if raw_output is not None else result
                )
        else:
            output = self._format_for_display(result)
            success = True

        truncated = self._truncate_lines(output, self.show_tool_lines)
        icon = "✅" if success else "❌"

        if agent_name:
            print(
                f"    \033[36m└─ {icon} [{agent_name}] Tool result: {tool_name}\033[0m"
            )
            indented = "\n".join(f"       {line}" for line in truncated.split("\n"))
            print(f"\033[2m{indented}\033[0m\n")
        else:
            print(f"\033[36m{icon} Tool result: {tool_name}\033[0m")
            indented = "\n".join(f"   {line}" for line in truncated.split("\n"))
            print(f"\033[2m{indented}\033[0m\n")

        self.last_activity = datetime.now()
        return HookResult(action="continue")

    def _format_for_display(self, value: Any) -> str:
        """Format any value for readable display."""
        if value is None:
            return "(none)"
        if isinstance(value, str):
            return value if value else "(empty)"
        if isinstance(value, (dict, list)):
            try:
                return self._to_yaml_style(value)
            except Exception:
                return str(value)
        return str(value)

    def _to_yaml_style(self, value: Any, indent: int = 0) -> str:
        """Convert value to YAML-style string."""
        prefix = "  " * indent

        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            if "\n" in value:
                lines = value.split("\n")
                return "|\n" + "\n".join(f"{prefix}  {line}" for line in lines)
            if value and value[0] not in "-?:,[]{}#&!|>'\"%@`" and ": " not in value:
                return value
            return f'"{value}"'
        if isinstance(value, list):
            if not value:
                return "[]"
            lines = []
            for item in value:
                if isinstance(item, dict):
                    for i, (k, v) in enumerate(item.items()):
                        formatted_v = self._to_yaml_style(v, indent + 1)
                        if i == 0:
                            lines.append(f"{prefix}- {k}: {formatted_v}")
                        else:
                            lines.append(f"{prefix}  {k}: {formatted_v}")
                else:
                    formatted = self._to_yaml_style(item, indent + 1)
                    lines.append(f"{prefix}- {formatted}")
            return "\n".join(lines)
        if isinstance(value, dict):
            if not value:
                return "{}"
            lines = []
            for k, v in value.items():
                if isinstance(v, (dict, list)) and v:
                    formatted = self._to_yaml_style(v, indent)
                    lines.append(f"{prefix}{k}:")
                    lines.append(formatted)
                else:
                    formatted = self._to_yaml_style(v, indent + 1)
                    lines.append(f"{prefix}{k}: {formatted}")
            return "\n".join(lines)
        return str(value)

    def _truncate_lines(self, text: str, max_lines: int) -> str:
        """Truncate text to max_lines with ellipsis."""
        if not isinstance(text, str):
            text = str(text) if text is not None else ""
        if not text:
            return "(empty)"
        lines = text.split("\n")
        if len(lines) == 1 and len(text) > 200:
            return text[:200] + f"... ({len(text) - 200} more chars)"
        if len(lines) <= max_lines:
            return text
        truncated = lines[:max_lines]
        remaining = len(lines) - max_lines
        truncated.append(f"... ({remaining} more lines)")
        return "\n".join(truncated)


def _flatten_reasoning_block(block: dict[str, Any]) -> str:
    """Flatten OpenAI reasoning block structures into plain text."""
    fragments: list[str] = []

    def _collect(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if value:
                fragments.append(value)
            return
        if isinstance(value, dict):
            _collect(value.get("text"))
            _collect(value.get("thinking"))
            _collect(value.get("summary"))
            _collect(value.get("content"))
            return
        if isinstance(value, list):
            for item in value:
                _collect(item)
            return
        text_attr = getattr(value, "text", None)
        if isinstance(text_attr, str) and text_attr:
            fragments.append(text_attr)

    _collect(block.get("thinking"))
    _collect(block.get("text"))
    _collect(block.get("summary"))
    _collect(block.get("content"))

    return "\n".join(fragment for fragment in fragments if fragment)


__all__ = ["mount", "StreamingUIHooks"]
