"""Streaming UI Hooks Module

Display streaming LLM output with Rich Console rendering:
- Session headers with elapsed time and cost
- Smart tool headers (type-aware: Edit, Bash, Task, Read, etc.)
- Nested session support with visual indentation
- Status bar showing current phase and metrics
- Colored diffs, error highlighting, result summaries

Requires: rich
"""

# Amplifier module metadata
__amplifier_module_type__ = "hook"

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from amplifier_core.models import HookResult

from .cost import estimate_cost
from .formatting import is_error_result
from .rich_output import (
    format_status_bar,
    print_session_footer,
    print_session_header,
    print_thinking_block,
    print_thinking_elapsed,
    print_token_usage,
    print_tool_call,
    print_tool_result,
)
from .state import Phase, StateManager, ToolCall
from .terminal import Terminal

logger = logging.getLogger(__name__)


async def mount(coordinator: Any, config: dict[str, Any]) -> None:
    """Mount streaming UI hooks module."""
    ui_config = config.get("ui", {})

    hooks = StreamingUIHooks(
        show_thinking=ui_config.get("show_thinking", True),
        show_tool_output=ui_config.get("show_tool_output", True),
        max_tool_lines=ui_config.get("max_tool_lines", 10),
        show_token_usage=ui_config.get("show_token_usage", True),
        show_status_bar=ui_config.get("show_status_bar", True),
        status_update_interval=ui_config.get("status_update_interval", 0.5),
    )

    # Register hooks
    coordinator.hooks.register("session:start", hooks.handle_session_start)
    coordinator.hooks.register("session:end", hooks.handle_session_end)
    coordinator.hooks.register("content_block:start", hooks.handle_content_block_start)
    coordinator.hooks.register("content_block:end", hooks.handle_content_block_end)
    coordinator.hooks.register("tool:pre", hooks.handle_tool_pre)
    coordinator.hooks.register("tool:post", hooks.handle_tool_post)
    coordinator.hooks.register("task:spawned", hooks.handle_task_spawned)
    coordinator.hooks.register("task:complete", hooks.handle_task_complete)

    logger.info("Mounted hooks-streaming-ui (Rich Console)")


class StreamingUIHooks:
    """Hooks for displaying streaming UI with Rich Console output."""

    def __init__(
        self,
        show_thinking: bool = True,
        show_tool_output: bool = True,
        max_tool_lines: int = 10,
        show_token_usage: bool = True,
        show_status_bar: bool = True,
        status_update_interval: float = 0.5,
    ):
        self.show_thinking = show_thinking
        self.show_tool_output = show_tool_output
        self.max_tool_lines = max_tool_lines
        self.show_token_usage = show_token_usage
        self.show_status_bar = show_status_bar
        self.status_update_interval = status_update_interval

        # State management
        self.state_manager = StateManager()
        self.terminal = Terminal()  # kept for scroll region / status bar only

        # Thinking block tracking
        self.thinking_blocks: dict[int, dict[str, Any]] = {}

        # Status bar thread
        self._status_thread: Optional[threading.Thread] = None
        self._status_running = False

        # Output lock coordinates Rich Console output with Terminal status bar.
        # Rich Console has internal locking, but status bar uses raw escape codes
        # through Terminal, so we need external coordination.
        self._output_lock = threading.Lock()

        # CWD for making paths relative in tool headers
        self._cwd: Path | None = None
        try:
            self._cwd = Path.cwd()
        except OSError:
            pass

    # ========================================================================
    # Session Lifecycle
    # ========================================================================

    async def handle_session_start(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle session start - print header, start status bar."""
        session_id = data.get("session_id", "")
        parent_id = data.get("parent_id")
        model = data.get("model")
        provider = data.get("provider")

        state = self.state_manager.get_or_create(
            session_id, parent_id, model=model, provider=provider
        )
        state.phase = Phase.IDLE

        # Print session header for root sessions
        if state.depth == 0:
            with self._output_lock:
                print_session_header(state)
            if self.show_status_bar:
                self._start_status_bar()

        return HookResult(action="continue")

    async def handle_session_end(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle session end - print footer, cleanup."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        if state:
            state.phase = Phase.COMPLETE

            # Calculate cost
            cost = None
            if state.provider and state.model:
                cost = estimate_cost(
                    state.metrics.input_tokens,
                    state.metrics.output_tokens,
                    state.provider,
                    state.model,
                    state.metrics.cache_read_tokens,
                    state.metrics.cache_create_tokens,
                )

            # Print footer for nested sessions
            if state.depth > 0:
                with self._output_lock:
                    print_session_footer(state, cost)

            # Stop status bar for root session
            if state.depth == 0 and self.show_status_bar:
                self._stop_status_bar()

        return HookResult(action="continue")

    # ========================================================================
    # Content Blocks (Thinking)
    # ========================================================================

    async def handle_content_block_start(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle content block start - detect thinking blocks."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        block_type = data.get("block_type")
        block_index = data.get("block_index")

        if block_type in {"thinking", "reasoning"} and self.show_thinking:
            if state:
                self.state_manager.transition(session_id, Phase.THINKING)
            if block_index is not None:
                self.thinking_blocks[block_index] = {
                    "started": True,
                    "session_id": session_id,
                    "start_time": datetime.now(),
                }

        return HookResult(action="continue")

    async def handle_content_block_end(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle content block end - display thinking, token usage."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        block_index = data.get("block_index")
        total_blocks = data.get("total_blocks")
        block = data.get("block", {})
        block_type = block.get("type")
        usage = data.get("usage")
        is_last_block = block_index == total_blocks - 1 if total_blocks else False

        depth = state.depth if state else 0

        # Handle thinking block completion
        if (
            block_type in {"thinking", "reasoning"}
            and block_index is not None
            and block_index in self.thinking_blocks
        ):
            thinking_info = self.thinking_blocks[block_index]
            thinking_text = (
                block.get("thinking", "")
                or block.get("text", "")
                or _flatten_content(block)
            )

            if thinking_text and self.show_thinking:
                with self._output_lock:
                    print_thinking_block(thinking_text, depth)

                    # Show elapsed thinking time
                    if thinking_info.get("start_time"):
                        elapsed = (
                            datetime.now() - thinking_info["start_time"]
                        ).total_seconds()
                        if elapsed > 1:
                            print_thinking_elapsed(elapsed, depth)

            del self.thinking_blocks[block_index]

            if state:
                self.state_manager.transition(session_id, Phase.STREAMING)

        # Handle token usage on last block
        if is_last_block and self.show_token_usage and usage:
            self._update_metrics(session_id, usage)

            if state:
                cost = None
                if state.provider and state.model:
                    cost = estimate_cost(
                        state.metrics.input_tokens,
                        state.metrics.output_tokens,
                        state.provider,
                        state.model,
                        state.metrics.cache_read_tokens,
                        state.metrics.cache_create_tokens,
                    )

                with self._output_lock:
                    print_token_usage(
                        state.metrics.input_tokens,
                        state.metrics.output_tokens,
                        state.metrics.cache_read_tokens,
                        state.metrics.cache_create_tokens,
                        state.elapsed_formatted(),
                        cost,
                        depth,
                    )

        return HookResult(action="continue")

    # ========================================================================
    # Tool Calls
    # ========================================================================

    async def handle_tool_pre(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle tool invocation - display smart tool header."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        tool_name = data.get("tool_name", "unknown")
        tool_input = data.get("tool_input", {})

        if state:
            state.phase = Phase.TOOL_RUNNING
            state.current_tool = ToolCall(name=tool_name, arguments=tool_input)
            state.metrics.tool_calls += 1

        depth = state.depth if state else 0
        with self._output_lock:
            print_tool_call(tool_name, tool_input, depth, self._cwd)

        return HookResult(action="continue")

    async def handle_tool_post(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle tool result - display result summary."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        tool_name = data.get("tool_name", "unknown")
        result = data.get("tool_response", data.get("result", {}))

        # Determine success
        success = not is_error_result(result)

        if state:
            state.phase = Phase.STREAMING
            state.current_tool = None

        if self.show_tool_output:
            depth = state.depth if state else 0
            with self._output_lock:
                print_tool_result(
                    tool_name, result, success, depth, self.max_tool_lines
                )

        return HookResult(action="continue")

    # ========================================================================
    # Nested Sessions (Task Delegation)
    # ========================================================================

    async def handle_task_spawned(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle sub-agent task spawn."""
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
            with self._output_lock:
                print_session_header(state)

        return HookResult(action="continue")

    async def handle_task_complete(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle sub-agent task completion."""
        session_id = data.get("session_id") or data.get("child_session_id")
        success = data.get("success", True)

        if not session_id:
            return HookResult(action="continue")

        state = self.state_manager.get(session_id)
        if state:
            state.phase = Phase.COMPLETE if success else Phase.ERROR

            cost = None
            if state.provider and state.model:
                cost = estimate_cost(
                    state.metrics.input_tokens,
                    state.metrics.output_tokens,
                    state.provider,
                    state.model,
                    state.metrics.cache_read_tokens,
                    state.metrics.cache_create_tokens,
                )

            with self._output_lock:
                print_session_footer(state, cost)

        return HookResult(action="continue")

    # ========================================================================
    # Internal Methods
    # ========================================================================

    def _update_metrics(self, session_id: str, usage: dict) -> None:
        """Update session metrics from usage data."""
        state = self.state_manager.get(session_id)
        if not state:
            return

        state.metrics.input_tokens += usage.get("input_tokens", 0)
        state.metrics.output_tokens += usage.get("output_tokens", 0)
        state.metrics.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        state.metrics.cache_create_tokens += usage.get("cache_creation_input_tokens", 0)

    def _start_status_bar(self) -> None:
        """Start background status bar updates."""
        if self._status_running:
            return

        self._status_running = True
        self.terminal.setup_scroll_region()
        self._status_thread = threading.Thread(target=self._status_loop, daemon=True)
        self._status_thread.start()

    def _stop_status_bar(self) -> None:
        """Stop status bar and restore terminal."""
        self._status_running = False
        if self._status_thread:
            self._status_thread.join(timeout=1.0)
            self._status_thread = None
        self.terminal.clear_status()
        self.terminal.teardown_scroll_region()

    def _status_loop(self) -> None:
        """Background loop to update status bar."""
        while self._status_running:
            state = self.state_manager.get_current()
            if state:
                status = format_status_bar(
                    state.phase,
                    state.elapsed_seconds(),
                    state.metrics.total_tokens,
                )
                with self._output_lock:
                    self.terminal.update_status(status)
            time.sleep(self.status_update_interval)


def _flatten_content(block: dict[str, Any]) -> str:
    """Flatten content from various block structures."""
    fragments: list[str] = []

    def collect(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            if value:
                fragments.append(value)
            return
        if isinstance(value, dict):
            collect(value.get("text"))
            collect(value.get("thinking"))
            collect(value.get("summary"))
            collect(value.get("content"))
            return
        if isinstance(value, list):
            for item in value:
                collect(item)
            return
        text_attr = getattr(value, "text", None)
        if isinstance(text_attr, str) and text_attr:
            fragments.append(text_attr)

    collect(block.get("thinking"))
    collect(block.get("text"))
    collect(block.get("summary"))
    collect(block.get("content"))

    return "\n".join(fragments)


__all__ = ["mount", "StreamingUIHooks", "StateManager", "Terminal"]
