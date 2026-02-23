"""Streaming UI Hooks Module

Display streaming LLM output with Rich Console rendering:
- Session headers with elapsed time and cost
- Smart tool headers (type-aware: Edit, Bash, Task, Read, etc.)
- Nested session support with visual indentation
- Colored diffs, error highlighting, result summaries

Requires: rich
"""

# Amplifier module metadata
__amplifier_module_type__ = "hook"

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from amplifier_core.models import HookResult

from .cost import estimate_cost
from .formatting import (
    INSIGHT_OPEN_PATTERN,
    extract_insight_blocks,
    format_code_change,
    format_tool_header,
    is_error_result,
)
from .insights import get_insight_instructions
from .rich_output import (
    print_code_change,
    print_inline_status,
    print_insight_block,
    print_session_footer,
    print_session_header,
    print_thinking_block,
    print_thinking_elapsed,
    print_thinking_start,
    print_token_usage,
    print_tool_call,
    print_tool_merged,
    print_tool_result,
    print_write_summary,
)
from .live_footer import LiveFooter
from .state import Phase, StateManager, ToolCall
from .status_bar import StatusBarProvider

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
        thinking_preview_lines=ui_config.get("thinking_preview_lines", 0),
        insight_mode=ui_config.get("insight_mode", "off"),
        show_diff=ui_config.get("show_diff", True),
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

    # Expose status bar for CLI toolbar integration (prompt_toolkit bottom_toolbar)
    if hooks.status_bar:
        coordinator.register_capability("status_bar", hooks.status_bar)

    logger.info("Mounted hooks-streaming-ui (Rich Console)")


class StreamingUIHooks:
    """Hooks for displaying streaming UI with Rich Console output."""

    # Fast tools -- buffer header, merge with result in tool:post
    SLOW_TOOLS = {"bash", "shell", "delegate"}

    def __init__(
        self,
        show_thinking: bool = True,
        show_tool_output: bool = True,
        max_tool_lines: int = 10,
        show_token_usage: bool = True,
        show_status_bar: bool = True,
        thinking_preview_lines: int = 0,
        insight_mode: str = "off",
        show_diff: bool = True,
    ):
        self.show_thinking = show_thinking
        self.show_tool_output = show_tool_output
        self.max_tool_lines = max_tool_lines
        self.show_token_usage = show_token_usage
        self.show_status_bar = show_status_bar
        self.thinking_preview_lines = thinking_preview_lines
        self.insight_mode = insight_mode
        self.show_diff = show_diff

        # State management
        self.state_manager = StateManager()

        # Thinking block tracking -- keyed by (session_id, block_index)
        self.thinking_blocks: dict[tuple[str, int], dict[str, Any]] = {}

        # CWD for making paths relative in tool headers
        self._cwd: Path | None = None
        try:
            self._cwd = Path.cwd()
        except OSError:
            pass

        # Activity footer (animated spinner + output serialization)
        self._footer = LiveFooter(enabled=show_status_bar)

        # Status bar provider (CLI reads this for prompt_toolkit toolbar)
        self.status_bar = StatusBarProvider() if show_status_bar else None

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
            with self._footer.output():
                print_session_header(state)

        self._update_status(session_id)

        # Inject insight instructions if configured (root sessions only)
        if state.depth == 0 and self.insight_mode != "off":
            instructions = get_insight_instructions(self.insight_mode)
            if instructions:
                return HookResult(
                    action="inject_context",
                    context_injection=instructions,
                    context_injection_role="system",
                    ephemeral=False,
                )

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

            # Print session footer (root gets summary line, nested gets completion line)
            with self._footer.output():
                print_session_footer(state, cost)

            # Inline status fallback (when CLI hasn't integrated prompt_toolkit toolbar)
            if self.status_bar and state.depth == 0:
                toolbar_text = self.status_bar.format_toolbar()
                if toolbar_text:
                    with self._footer.output():
                        print_inline_status(toolbar_text)

            # Cleanly shut down footer for root session to prevent
            # "Unhandled exception in event loop" during interpreter exit
            if state.depth == 0:
                self._footer.shutdown()

        self._update_status(session_id)
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
                key = (session_id, block_index)
                self.thinking_blocks[key] = {
                    "started": True,
                    "session_id": session_id,
                    "start_time": datetime.now(),
                }
                # Print compact thinking indicator
                depth = state.depth if state else 0
                with self._footer.output():
                    print_thinking_start(depth)
                # Start animated footer
                self._footer.show("Thinking...", depth)

        self._update_status(session_id)
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

        # Handle insight blocks in text output
        if (
            block_type == "text"
            and self.insight_mode != "off"
            and INSIGHT_OPEN_PATTERN.search(block.get("text", ""))
        ):
            insights, remaining = extract_insight_blocks(block.get("text", ""))
            if insights:
                with self._footer.output():
                    for insight in insights:
                        print_insight_block(insight, depth)
                if not remaining:
                    return HookResult(action="continue")
                # Strip insight blocks from the text, pass remaining through
                modified_block = {**block, "text": remaining}
                modified_data = {**data, "block": modified_block}
                return HookResult(action="modify", data=modified_data)

        # Suppress verbose text blocks from sub-agents
        if block_type == "text" and state and state.depth > 0:
            return HookResult(action="continue")

        # Handle thinking block completion
        key = (session_id, block_index) if block_index is not None else None
        if (
            block_type in {"thinking", "reasoning"}
            and key is not None
            and key in self.thinking_blocks
        ):
            thinking_info = self.thinking_blocks[key]

            # Stop footer before printing
            self._footer.hide()

            # Extract thinking text for accordion preview
            thinking_text = ""
            if block_type == "thinking":
                thinking_text = block.get("thinking", "")
            elif block_type == "reasoning":
                # Reasoning blocks may have summary or content lists
                summary = block.get("summary", [])
                content = block.get("content", [])
                parts = []
                for item in summary if isinstance(summary, list) else [summary]:
                    if isinstance(item, dict):
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                for item in content if isinstance(content, list) else [content]:
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
                elapsed = (datetime.now() - thinking_info["start_time"]).total_seconds()
                if elapsed > 1:
                    with self._footer.output():
                        print_thinking_elapsed(elapsed, depth)
                        # Show preview if configured (0 = no preview, default)
                        if self.thinking_preview_lines > 0 and thinking_text:
                            print_thinking_block(
                                thinking_text, depth, self.thinking_preview_lines
                            )

            del self.thinking_blocks[key]

            if state:
                self.state_manager.transition(session_id, Phase.STREAMING)

        # Handle token usage on last block (root session only)
        if is_last_block and self.show_token_usage and usage:
            self._update_metrics(session_id, usage)

            if state and state.depth == 0:
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

                with self._footer.output():
                    print_token_usage(
                        state.metrics.input_tokens,
                        state.metrics.output_tokens,
                        state.metrics.cache_read_tokens,
                        state.metrics.cache_create_tokens,
                        state.elapsed_formatted(),
                        cost,
                        depth,
                    )

        self._update_status(session_id)
        return HookResult(action="continue")

    # ========================================================================
    # Tool Calls
    # ========================================================================

    async def handle_tool_pre(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle tool invocation -- display smart tool header."""
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
            with self._footer.output():
                print_tool_call(tool_name, tool_input, depth, self._cwd)
            if state:
                state.pending_tool_header = None
            self._footer.show(
                format_tool_header(tool_name, tool_input, self._cwd), depth
            )
        else:
            # Fast tools: buffer header for single-line merge in tool:post
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

        self._update_status(session_id)
        return HookResult(action="continue")

    async def handle_tool_post(self, _event: str, data: dict[str, Any]) -> HookResult:
        """Handle tool result -- display result summary."""
        session_id = data.get("session_id", "")
        state = self.state_manager.get(session_id)

        tool_name = data.get("tool_name", "unknown")
        result = data.get("tool_response", data.get("result", {}))
        success = not is_error_result(result)

        # Stop any active footer
        self._footer.hide()

        # Save tool input before clearing (needed for diff display)
        _tool_input_for_display = (
            state.current_tool.arguments if state and state.current_tool else None
        ) or data.get("tool_input", {})

        if state:
            state.phase = Phase.STREAMING
            state.current_tool = None

        if self.show_tool_output:
            depth = state.depth if state else 0
            tool_input = _tool_input_for_display

            with self._footer.output():
                # Claude-style inline diff for edit_file
                if (
                    self.show_diff
                    and tool_name.lower() == "edit_file"
                    and success
                    and isinstance(tool_input, dict)
                    and tool_input.get("old_string")
                ):
                    change = format_code_change(
                        file_path=tool_input.get("file_path", "?"),
                        old_string=tool_input.get("old_string", ""),
                        new_string=tool_input.get("new_string", ""),
                        cwd=self._cwd,
                    )
                    print_code_change(change, depth)
                    if state:
                        state.pending_tool_header = None

                # Write summary for write_file
                elif (
                    self.show_diff
                    and tool_name.lower() == "write_file"
                    and success
                    and isinstance(tool_input, dict)
                ):
                    file_path = tool_input.get("file_path", "?")
                    content_text = tool_input.get("content", "")
                    line_count = content_text.count("\n") + 1 if content_text else 0
                    from .formatting import make_relative

                    rel_path = make_relative(file_path, self._cwd)
                    print_write_summary(rel_path, line_count, depth)
                    if state:
                        state.pending_tool_header = None

                elif state and state.pending_tool_header:
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
                    # Header was already printed (slow tool) -- show result only
                    print_tool_result(
                        tool_name, result, success, depth, self.max_tool_lines
                    )

        self._update_status(session_id)
        return HookResult(action="continue")

    # ========================================================================
    # Nested Sessions (Task Delegation)
    # ========================================================================

    async def handle_task_spawned(
        self, _event: str, data: dict[str, Any]
    ) -> HookResult:
        """Handle sub-agent task spawn -- create child session with agent metadata."""
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
            parent = (
                self.state_manager.get(parent_session_id) if parent_session_id else None
            )
            if (
                parent
                and hasattr(parent, "_pending_agent_info")
                and parent._pending_agent_info
            ):
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
                    state.agent_name = (
                        agent_str.replace("-", " ").title() if agent_str else None
                    )

                # Short description from instruction
                if instruction:
                    state.agent_desc = (
                        instruction[:60] + "..."
                        if len(instruction) > 60
                        else instruction
                    )

                parent._pending_agent_info = None

            with self._footer.output():
                print_session_header(state)

            self._update_status(child_session_id)

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

            with self._footer.output():
                print_session_footer(state, cost)

        self._update_status(session_id)
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

    def _update_status(self, session_id: str) -> None:
        """Push current state to the status bar provider."""
        if not self.status_bar:
            return

        state = self.state_manager.get(session_id)
        if not state:
            return

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
            current_tool=(state.current_tool.name if state.current_tool else ""),
            input_tokens=total_input,
            output_tokens=state.metrics.output_tokens,
            cache_pct=cache_pct,
            elapsed=elapsed,
            model=state.model or "",
        )


__all__ = ["mount", "StreamingUIHooks", "StateManager", "StatusBarProvider"]
