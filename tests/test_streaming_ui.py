"""Tests for streaming UI hooks module.

Tests verify the hook contract (returns HookResult with correct action)
and output behavior (Rich Console to stderr).
"""

import re
from io import StringIO
from unittest.mock import MagicMock

import pytest
from amplifier_core import HookResult
from rich.console import Console

from amplifier_module_hooks_streaming_ui import StreamingUIHooks, mount
from amplifier_module_hooks_streaming_ui import rich_output

# Regex to strip ANSI escape codes from captured Rich Console output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hooks(**kwargs) -> StreamingUIHooks:
    """Create a StreamingUIHooks with sensible test defaults."""
    defaults = {
        "show_thinking": True,
        "show_tool_output": True,
        "max_tool_lines": 5,
        "show_token_usage": True,
        "show_status_bar": False,  # disable spinner/status bar threads in tests
        "thinking_preview_lines": 0,
    }
    defaults.update(kwargs)
    return StreamingUIHooks(**defaults)


def _capture_console() -> tuple[Console, StringIO]:
    """Create a capturing Console and inject it into rich_output."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    rich_output.set_console(console)
    return console, buf


def _get_output(buf: StringIO) -> str:
    """Get all captured output with ANSI escape codes stripped."""
    return _ANSI_RE.sub("", buf.getvalue())


def _make_hooks_with_session(session_id: str = "test-session", **kwargs) -> StreamingUIHooks:
    """Create hooks with a pre-registered session state."""
    hooks = _make_hooks(**kwargs)
    hooks.state_manager.get_or_create(session_id, parent_id=None)
    return hooks


# ---------------------------------------------------------------------------
# Mount Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_registers_hooks():
    """Test that mount registers all required hooks."""
    coordinator = MagicMock()
    coordinator.hooks = MagicMock()
    coordinator.hooks.register = MagicMock()

    config = {"ui": {"show_thinking": True, "max_tool_lines": 5, "show_token_usage": True}}
    await mount(coordinator, config)

    expected_events = [
        "content_block:start",
        "content_block:end",
        "tool:pre",
        "tool:post",
        "session:start",
        "session:end",
        "task:spawned",
        "task:complete",
    ]
    for event in expected_events:
        registered = any(
            call[0][0] == event for call in coordinator.hooks.register.call_args_list
        )
        assert registered, f"Event {event} was not registered"


@pytest.mark.asyncio
async def test_mount_with_defaults():
    """Test mount works with default config."""
    coordinator = MagicMock()
    coordinator.hooks = MagicMock()
    coordinator.hooks.register = MagicMock()

    await mount(coordinator, {})

    assert coordinator.hooks.register.call_count == 8


# ---------------------------------------------------------------------------
# Thinking Block Tests
# ---------------------------------------------------------------------------


class TestThinkingBlocks:
    """Test thinking block detection and display."""

    @pytest.mark.asyncio
    async def test_thinking_block_start(self):
        """Test thinking block start detection."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()
        data = {"block_type": "thinking", "block_index": 0, "session_id": "test-session"}

        result = await hooks.handle_content_block_start("content_block:start", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert ("test-session", 0) in hooks.thinking_blocks

    @pytest.mark.asyncio
    async def test_thinking_block_disabled(self):
        """Test thinking blocks are not tracked when disabled."""
        hooks = _make_hooks(show_thinking=False)
        data = {"block_type": "thinking", "block_index": 0, "session_id": "test-session"}

        result = await hooks.handle_content_block_start("content_block:start", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert ("test-session", 0) not in hooks.thinking_blocks

    @pytest.mark.asyncio
    async def test_thinking_block_end_compact(self):
        """Test thinking block end in compact mode (no preview text)."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()
        # Key is now (session_id, block_index)
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "block": {"type": "thinking", "thinking": "This is a test thought process."},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert ("test-session", 0) not in hooks.thinking_blocks

        output = _get_output(buf)
        # Compact mode: no thinking text shown (thinking_preview_lines=0)
        assert "This is a test thought process." not in output

    @pytest.mark.asyncio
    async def test_thinking_block_end_with_preview(self):
        """Test thinking block end with preview lines enabled."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(thinking_preview_lines=3)
        from datetime import datetime, timedelta
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
            "start_time": datetime.now() - timedelta(seconds=5),
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "block": {"type": "thinking", "thinking": "Line one\nLine two\nLine three\nLine four"},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        # With preview=3, first 3 lines should be shown
        assert "Line one" in output
        assert "Line two" in output
        assert "Line three" in output
        # Line four should NOT be shown (beyond preview limit)
        assert "Line four" not in output
        # Should show remaining count
        assert "+1 lines" in output

    @pytest.mark.asyncio
    async def test_reasoning_block_end(self):
        """Reasoning blocks should extract text from summary/content lists."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(thinking_preview_lines=3)
        from datetime import datetime, timedelta
        hooks.thinking_blocks[("test-session", 1)] = {
            "started": True,
            "session_id": "test-session",
            "start_time": datetime.now() - timedelta(seconds=5),
        }

        data = {
            "session_id": "test-session",
            "block_index": 1,
            "block": {
                "type": "reasoning",
                "summary": [{"text": "Summary insight"}],
                "content": [{"text": "Detailed chain of thought"}],
            },
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert ("test-session", 1) not in hooks.thinking_blocks

        output = _get_output(buf)
        assert "Summary insight" in output or "Detailed chain of thought" in output

    @pytest.mark.asyncio
    async def test_thinking_text_accumulated_on_state(self):
        """Test that thinking text is stored on session state."""
        hooks = _make_hooks_with_session()
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "block": {"type": "thinking", "thinking": "Deep thoughts here."},
        }

        await hooks.handle_content_block_end("content_block:end", data)

        state = hooks.state_manager.get("test-session")
        assert state is not None
        assert "Deep thoughts here." in state.thinking_text

    @pytest.mark.asyncio
    async def test_non_thinking_blocks_ignored(self):
        """Test that non-thinking blocks are ignored."""
        hooks = _make_hooks()
        data = {"block_type": "text", "block_index": 0, "session_id": "test-session"}

        result = await hooks.handle_content_block_start("content_block:start", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert ("test-session", 0) not in hooks.thinking_blocks

    @pytest.mark.asyncio
    async def test_thinking_blocks_keyed_by_session(self):
        """Test thinking blocks use (session_id, block_index) key to avoid collision."""
        hooks = _make_hooks_with_session("session-a")
        hooks.state_manager.get_or_create("session-b", parent_id=None)

        # Start thinking block 0 in both sessions
        await hooks.handle_content_block_start(
            "content_block:start",
            {"block_type": "thinking", "block_index": 0, "session_id": "session-a"},
        )
        await hooks.handle_content_block_start(
            "content_block:start",
            {"block_type": "thinking", "block_index": 0, "session_id": "session-b"},
        )

        assert ("session-a", 0) in hooks.thinking_blocks
        assert ("session-b", 0) in hooks.thinking_blocks

        # End one without affecting the other
        await hooks.handle_content_block_end(
            "content_block:end",
            {
                "session_id": "session-a",
                "block_index": 0,
                "block": {"type": "thinking", "thinking": "A"},
            },
        )

        assert ("session-a", 0) not in hooks.thinking_blocks
        assert ("session-b", 0) in hooks.thinking_blocks


# ---------------------------------------------------------------------------
# Tool Call Tests
# ---------------------------------------------------------------------------


class TestToolCalls:
    """Test tool invocation and result display."""

    @pytest.mark.asyncio
    async def test_fast_tool_buffered(self):
        """Fast tools (read_file) buffer header and merge in tool:post."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        # tool:pre for a fast tool -- should NOT print anything
        pre_data = {
            "session_id": "test-session",
            "tool_name": "read_file",
            "tool_input": {"file_path": "/some/path/to/file.txt"},
        }
        result = await hooks.handle_tool_pre("tool:pre", pre_data)
        assert result.action == "continue"

        pre_output = _get_output(buf)
        # Fast tool: header is buffered, not printed yet
        assert "Read:" not in pre_output

        # Verify header is buffered on state
        state = hooks.state_manager.get("test-session")
        assert state is not None
        assert state.pending_tool_header is not None
        assert "Read:" in state.pending_tool_header

        # tool:post -- should print merged single-line output
        post_data = {
            "session_id": "test-session",
            "tool_name": "read_file",
            "tool_response": {"stdout": "line1\nline2\nline3\n", "success": True},
        }
        result = await hooks.handle_tool_post("tool:post", post_data)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "Read:" in output
        assert "file.txt" in output

    @pytest.mark.asyncio
    async def test_slow_tool_immediate(self):
        """Slow tools (bash) print header immediately in tool:pre."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        data = {
            "session_id": "test-session",
            "tool_name": "bash",
            "tool_input": {"command": "npm test"},
        }

        result = await hooks.handle_tool_pre("tool:pre", data)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "Bash:" in output
        assert "npm test" in output

        # Verify no buffered header
        state = hooks.state_manager.get("test-session")
        assert state is not None
        assert state.pending_tool_header is None

    @pytest.mark.asyncio
    async def test_tool_pre_edit_file_buffered(self):
        """edit_file is a fast tool -- header is buffered."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        data = {
            "session_id": "test-session",
            "tool_name": "edit_file",
            "tool_input": {
                "file_path": "src/auth.py",
                "old_string": "old_code()",
                "new_string": "new_code()\nextra_line()",
            },
        }

        await hooks.handle_tool_pre("tool:pre", data)

        output = _get_output(buf)
        # Fast tool: no output from tool:pre
        assert "Edit:" not in output

        state = hooks.state_manager.get("test-session")
        assert state is not None
        assert state.pending_tool_header is not None
        assert "Edit:" in state.pending_tool_header

    @pytest.mark.asyncio
    async def test_tool_post_success(self):
        """Test successful tool result display for slow tool."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        # Simulate slow tool (no pending header)
        data = {
            "session_id": "test-session",
            "tool_name": "bash",
            "tool_response": {"success": True, "returncode": 0, "stdout": "OK"},
        }

        result = await hooks.handle_tool_post("tool:post", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

    @pytest.mark.asyncio
    async def test_tool_post_failure(self):
        """Test failed tool result display shows error."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        data = {
            "session_id": "test-session",
            "tool_name": "bash",
            "tool_response": {
                "returncode": 1,
                "stdout": "",
                "stderr": "Error: command not found",
            },
        }

        result = await hooks.handle_tool_post("tool:post", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "(error)" in output
        assert "Error: command not found" in output

    @pytest.mark.asyncio
    async def test_tool_merged_output_for_fast_tool(self):
        """Test the full fast-tool cycle: pre (buffer) + post (merged line)."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        # tool:pre buffers the header
        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "glob",
                "tool_input": {"pattern": "**/*.py"},
            },
        )

        # tool:post prints merged single line
        await hooks.handle_tool_post(
            "tool:post",
            {
                "session_id": "test-session",
                "tool_name": "glob",
                "tool_response": {"files": ["a.py", "b.py"], "total_files": 2},
            },
        )

        output = _get_output(buf)
        assert "Glob:" in output

    @pytest.mark.asyncio
    async def test_tool_with_string_result(self):
        """Test tool result when result is a plain string."""
        _console, buf = _capture_console()
        hooks = _make_hooks()

        data = {"tool_name": "some_tool", "tool_response": "Simple string result"}

        result = await hooks.handle_tool_post("tool:post", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"


# ---------------------------------------------------------------------------
# Token Usage Tests
# ---------------------------------------------------------------------------


class TestTokenUsage:
    """Test token usage display."""

    @pytest.mark.asyncio
    async def test_token_usage_displayed_on_last_block(self):
        """Test token usage displayed after last content block."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "thinking", "thinking": "Test thinking"},
            "usage": {
                "input_tokens": 1234,
                "output_tokens": 567,
            },
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "1,234" in output
        assert "567" in output

    @pytest.mark.asyncio
    async def test_token_usage_not_displayed_for_non_last_block(self):
        """Test token usage NOT displayed for blocks that aren't last."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 2,
            "block": {"type": "thinking", "thinking": "Test"},
            "usage": {"input_tokens": 1234, "output_tokens": 567},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "1,234" not in output

    @pytest.mark.asyncio
    async def test_token_usage_disabled(self):
        """Test token usage is not shown when disabled."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(show_token_usage=False)
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "thinking", "thinking": "Test"},
            "usage": {"input_tokens": 1234, "output_tokens": 567},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "1,234" not in output

    @pytest.mark.asyncio
    async def test_token_usage_missing_from_event(self):
        """Test token usage handles missing usage data gracefully."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()
        hooks.thinking_blocks[("test-session", 0)] = {
            "started": True,
            "session_id": "test-session",
        }

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "thinking", "thinking": "Test"},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "\u2193" not in output


# ---------------------------------------------------------------------------
# Agent Tree Tests
# ---------------------------------------------------------------------------


class TestAgentTree:
    """Test agent tree headers and metadata transfer."""

    @pytest.mark.asyncio
    async def test_agent_info_transferred_to_child(self):
        """Test that delegate tool passes agent info to child session."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        # Parent calls delegate tool
        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "delegate",
                "tool_input": {
                    "agent": "foundation:explorer",
                    "instruction": "Survey the authentication module structure",
                },
            },
        )

        # Child session spawned
        await hooks.handle_task_spawned(
            "task:spawned",
            {
                "child_session_id": "child-1",
                "parent_session_id": "test-session",
            },
        )

        child = hooks.state_manager.get("child-1")
        assert child is not None
        assert child.agent_name == "Explorer"
        assert child.agent_type == "explorer"
        assert "Survey the authentication" in (child.agent_desc or "")

        output = _get_output(buf)
        assert "Explorer" in output

    @pytest.mark.asyncio
    async def test_depth_prefix_in_nested_output(self):
        """Test that nested sessions get depth-colored prefixes."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session()

        # Create nested session
        hooks.state_manager.get_or_create("child-1", parent_id="test-session")

        child = hooks.state_manager.get("child-1")
        assert child is not None
        assert child.depth == 1

    @pytest.mark.asyncio
    async def test_breadcrumb_building(self):
        """Test breadcrumb path generation."""
        hooks = _make_hooks_with_session()
        hooks.state_manager.get_or_create("child-1", parent_id="test-session")

        child = hooks.state_manager.get("child-1")
        assert child is not None
        child.agent_name = "Explorer"

        breadcrumb = hooks.state_manager.get_breadcrumb("child-1")
        assert "main" in breadcrumb
        assert "Explorer" in breadcrumb
        assert "\u2192" in breadcrumb


# ---------------------------------------------------------------------------
# Spinner Tests
# ---------------------------------------------------------------------------


class TestSpinner:
    """Test spinner behavior."""

    def test_spinner_not_created_when_disabled(self):
        """Spinner should not be created when show_status_bar=False."""
        hooks = _make_hooks(show_status_bar=False)
        assert hooks._spinner is None

    def test_spinner_created_when_enabled(self):
        """Spinner should be created when show_status_bar=True."""
        hooks = _make_hooks(show_status_bar=True)
        assert hooks._spinner is not None


# ---------------------------------------------------------------------------
# Status Bar Tests
# ---------------------------------------------------------------------------


class TestStatusBar:
    """Test status bar provider."""

    def test_status_bar_not_created_when_disabled(self):
        """Status bar should not be created when show_status_bar=False."""
        hooks = _make_hooks(show_status_bar=False)
        assert hooks.status_bar is None

    def test_status_bar_created_when_enabled(self):
        """Status bar should be created when show_status_bar=True."""
        hooks = _make_hooks(show_status_bar=True)
        assert hooks.status_bar is not None

    @pytest.mark.asyncio
    async def test_status_bar_updated_on_events(self):
        """Test that status bar is updated after events."""
        hooks = StreamingUIHooks(show_status_bar=True)
        hooks.state_manager.get_or_create("test-session", parent_id=None, model="claude-opus-4-6")

        await hooks.handle_session_start(
            "session:start",
            {"session_id": "test-session", "model": "claude-opus-4-6"},
        )

        assert hooks.status_bar is not None
        status = hooks.status_bar.get_status()
        assert status.phase == "Ready"

    def test_status_bar_format_toolbar(self):
        """Test toolbar formatting."""
        from amplifier_module_hooks_streaming_ui.status_bar import StatusBarProvider
        sb = StatusBarProvider()
        sb.update(phase="Thinking", input_tokens=50000, output_tokens=1000, elapsed="00:15")

        toolbar = sb.format_toolbar()
        assert "Thinking" in toolbar
        assert "50.0k" in toolbar
        assert "00:15" in toolbar


# ---------------------------------------------------------------------------
# Insight Injection Tests
# ---------------------------------------------------------------------------


class TestInsightInjection:
    """Test insight mode context injection on session:start."""

    @pytest.mark.asyncio
    async def test_insight_off_returns_continue(self):
        """With insight_mode='off', session:start returns continue (no injection)."""
        hooks = _make_hooks(insight_mode="off")
        hooks.state_manager.get_or_create("test-session", parent_id=None)

        result = await hooks.handle_session_start(
            "session:start", {"session_id": "test-session"}
        )

        assert isinstance(result, HookResult)
        assert result.action == "continue"

    @pytest.mark.asyncio
    async def test_insight_explanatory_injects_context(self):
        """With insight_mode='explanatory', session:start injects system context."""
        hooks = _make_hooks(insight_mode="explanatory")
        hooks.state_manager.get_or_create("test-session", parent_id=None)

        result = await hooks.handle_session_start(
            "session:start", {"session_id": "test-session"}
        )

        assert isinstance(result, HookResult)
        assert result.action == "inject_context"
        assert result.context_injection is not None
        assert "\u2605 Insight" in result.context_injection
        assert result.context_injection_role == "system"

    @pytest.mark.asyncio
    async def test_insight_learning_injects_context(self):
        """With insight_mode='learning', instructions include learning content."""
        hooks = _make_hooks(insight_mode="learning")
        hooks.state_manager.get_or_create("test-session", parent_id=None)

        result = await hooks.handle_session_start(
            "session:start", {"session_id": "test-session"}
        )

        assert isinstance(result, HookResult)
        assert result.action == "inject_context"
        assert "5-10 lines" in result.context_injection

    @pytest.mark.asyncio
    async def test_insight_combined_injects_both(self):
        """Combined mode includes both learning and insight block content."""
        hooks = _make_hooks(insight_mode="combined")
        hooks.state_manager.get_or_create("test-session", parent_id=None)

        result = await hooks.handle_session_start(
            "session:start", {"session_id": "test-session"}
        )

        assert isinstance(result, HookResult)
        assert result.action == "inject_context"
        assert "\u2605 Insight" in result.context_injection
        assert "learning" in result.context_injection.lower()

    @pytest.mark.asyncio
    async def test_insight_not_injected_for_child_sessions(self):
        """Insights are only injected for root sessions (depth=0)."""
        hooks = _make_hooks(insight_mode="explanatory")
        hooks.state_manager.get_or_create("parent", parent_id=None)
        hooks.state_manager.get_or_create("child", parent_id="parent")

        result = await hooks.handle_session_start(
            "session:start", {"session_id": "child", "parent_id": "parent"}
        )

        assert isinstance(result, HookResult)
        assert result.action == "continue"


# ---------------------------------------------------------------------------
# Code Change Display Tests
# ---------------------------------------------------------------------------


class TestCodeChangeDisplay:
    """Test Claude-style inline diff rendering for edit_file."""

    @pytest.mark.asyncio
    async def test_edit_file_shows_diff(self):
        """edit_file with show_diff=True renders inline diff."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(show_diff=True)

        # Simulate tool:pre (buffers header for fast tool)
        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {
                    "file_path": "src/auth.py",
                    "old_string": 'description = "old text"',
                    "new_string": 'description = "new text"',
                },
            },
        )

        # Simulate tool:post (success)
        await hooks.handle_tool_post(
            "tool:post",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {
                    "file_path": "src/auth.py",
                    "old_string": 'description = "old text"',
                    "new_string": 'description = "new text"',
                },
                "tool_response": {"success": True},
            },
        )

        output = _get_output(buf)
        assert "Update(src/auth.py)" in output
        assert "Added 1 line" in output
        assert "removed 1 line" in output
        assert "old text" in output
        assert "new text" in output

    @pytest.mark.asyncio
    async def test_edit_file_diff_disabled(self):
        """edit_file with show_diff=False uses normal merged output."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(show_diff=False)

        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {
                    "file_path": "src/auth.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            },
        )

        await hooks.handle_tool_post(
            "tool:post",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {
                    "file_path": "src/auth.py",
                    "old_string": "old",
                    "new_string": "new",
                },
                "tool_response": {"success": True},
            },
        )

        output = _get_output(buf)
        # Should NOT show Claude-style Update() header
        assert "Update(" not in output
        # Should show normal merged header instead
        assert "Edit:" in output

    @pytest.mark.asyncio
    async def test_edit_file_error_falls_through(self):
        """edit_file errors bypass diff display and show normal error output."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(show_diff=True)

        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {
                    "file_path": "src/auth.py",
                    "old_string": "old",
                    "new_string": "new",
                },
            },
        )

        await hooks.handle_tool_post(
            "tool:post",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {
                    "file_path": "src/auth.py",
                    "old_string": "old",
                    "new_string": "new",
                },
                "tool_response": {"error": "old_string not found"},
            },
        )

        output = _get_output(buf)
        # Error: should fall through to normal merged output, not diff
        assert "(error)" in output

    @pytest.mark.asyncio
    async def test_write_file_shows_summary(self):
        """write_file with show_diff=True renders write summary."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(show_diff=True)

        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "write_file",
                "tool_input": {
                    "file_path": "src/new_file.py",
                    "content": "line1\nline2\nline3\n",
                },
            },
        )

        await hooks.handle_tool_post(
            "tool:post",
            {
                "session_id": "test-session",
                "tool_name": "write_file",
                "tool_input": {
                    "file_path": "src/new_file.py",
                    "content": "line1\nline2\nline3\n",
                },
                "tool_response": {"success": True},
            },
        )

        output = _get_output(buf)
        assert "Write(src/new_file.py)" in output
        assert "Created" in output
        assert "4 lines" in output

    @pytest.mark.asyncio
    async def test_multiline_edit_shows_context(self):
        """Multi-line edit shows context lines around changes."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(show_diff=True)

        old = "line1\nline2\nold_line\nline4\nline5"
        new = "line1\nline2\nnew_line\nline4\nline5"

        await hooks.handle_tool_pre(
            "tool:pre",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {"file_path": "f.py", "old_string": old, "new_string": new},
            },
        )

        await hooks.handle_tool_post(
            "tool:post",
            {
                "session_id": "test-session",
                "tool_name": "edit_file",
                "tool_input": {"file_path": "f.py", "old_string": old, "new_string": new},
                "tool_response": {"success": True},
            },
        )

        output = _get_output(buf)
        assert "Update(f.py)" in output
        # Context lines present
        assert "line1" in output
        assert "line2" in output
        # Changed lines
        assert "old_line" in output
        assert "new_line" in output


# ---------------------------------------------------------------------------
# Formatting: format_code_change Tests
# ---------------------------------------------------------------------------


class TestFormatCodeChange:
    """Test the pure formatting logic for code change diffs."""

    def test_single_line_replacement(self):
        from amplifier_module_hooks_streaming_ui.formatting import format_code_change

        change = format_code_change("test.py", "old", "new")
        assert change.display_path == "test.py"
        assert change.additions == 1
        assert change.deletions == 1
        assert "Added 1 line" in change.summary
        assert "removed 1 line" in change.summary
        assert len(change.diff_lines) == 2
        assert change.diff_lines[0].marker == "-"
        assert change.diff_lines[1].marker == "+"

    def test_pure_addition(self):
        from amplifier_module_hooks_streaming_ui.formatting import format_code_change

        change = format_code_change("test.py", "a\nb", "a\nb\nc")
        assert change.additions == 1
        assert change.deletions == 0
        assert "Added 1 line" in change.summary
        assert "removed" not in change.summary

    def test_pure_deletion(self):
        from amplifier_module_hooks_streaming_ui.formatting import format_code_change

        change = format_code_change("test.py", "a\nb\nc", "a\nb")
        assert change.additions == 0
        assert change.deletions == 1
        assert "removed 1 line" in change.summary

    def test_context_lines_included(self):
        from amplifier_module_hooks_streaming_ui.formatting import format_code_change

        old = "ctx1\nctx2\nctx3\nold\nctx4\nctx5\nctx6"
        new = "ctx1\nctx2\nctx3\nnew\nctx4\nctx5\nctx6"
        change = format_code_change("test.py", old, new, context_lines=2)

        # Should have context lines with " " marker
        context = [dl for dl in change.diff_lines if dl.marker == " "]
        assert len(context) >= 2
        changed = [dl for dl in change.diff_lines if dl.marker in ("-", "+")]
        assert len(changed) == 2

    def test_empty_old_string(self):
        from amplifier_module_hooks_streaming_ui.formatting import format_code_change

        change = format_code_change("test.py", "", "new line")
        assert change.additions == 1
        assert change.deletions == 0

    def test_path_made_relative(self):
        from pathlib import Path
        from amplifier_module_hooks_streaming_ui.formatting import format_code_change

        cwd = Path("/home/user/project")
        change = format_code_change("/home/user/project/src/auth.py", "a", "b", cwd=cwd)
        assert change.display_path == "src/auth.py"


# ---------------------------------------------------------------------------
# Insights Module Tests
# ---------------------------------------------------------------------------


class TestInsightsModule:
    """Test the insights module directly."""

    def test_get_insight_explanatory(self):
        from amplifier_module_hooks_streaming_ui.insights import get_insight_instructions
        result = get_insight_instructions("explanatory")
        assert result is not None
        assert "\u2605 Insight" in result

    def test_get_insight_learning(self):
        from amplifier_module_hooks_streaming_ui.insights import get_insight_instructions
        result = get_insight_instructions("learning")
        assert result is not None
        assert "5-10 lines" in result

    def test_get_insight_combined(self):
        from amplifier_module_hooks_streaming_ui.insights import get_insight_instructions
        result = get_insight_instructions("combined")
        assert result is not None
        assert "\u2605 Insight" in result
        assert "learning" in result.lower()

    def test_get_insight_off_returns_none(self):
        from amplifier_module_hooks_streaming_ui.insights import get_insight_instructions
        result = get_insight_instructions("off")
        assert result is None

    def test_get_insight_unknown_returns_none(self):
        from amplifier_module_hooks_streaming_ui.insights import get_insight_instructions
        result = get_insight_instructions("banana")
        assert result is None


# ---------------------------------------------------------------------------
# Insight Block Rendering Tests
# ---------------------------------------------------------------------------


class TestInsightBlockRendering:
    """Test insight block extraction, rendering, and handler integration."""

    def test_extract_insight_blocks_single(self):
        """Single insight block is extracted cleanly."""
        from amplifier_module_hooks_streaming_ui.formatting import extract_insight_blocks

        text = (
            "`★ Insight ─────────────────────────────────`\n"
            "Key point one\nKey point two\n"
            "`─────────────────────────────────────────────`"
        )
        insights, remaining = extract_insight_blocks(text)
        assert len(insights) == 1
        assert "Key point one" in insights[0]
        assert "Key point two" in insights[0]
        assert remaining == ""

    def test_extract_insight_blocks_embedded(self):
        """Insight block surrounded by regular text returns both parts."""
        from amplifier_module_hooks_streaming_ui.formatting import extract_insight_blocks

        text = (
            "Some preamble text.\n\n"
            "`★ Insight ─────────────────────────────────`\n"
            "Educational content here\n"
            "`─────────────────────────────────────────────`\n\n"
            "Some trailing text."
        )
        insights, remaining = extract_insight_blocks(text)
        assert len(insights) == 1
        assert "Educational content" in insights[0]
        assert "preamble" in remaining
        assert "trailing" in remaining

    def test_extract_no_insight_blocks(self):
        """Plain text with no insight delimiters passes through unchanged."""
        from amplifier_module_hooks_streaming_ui.formatting import extract_insight_blocks

        text = "Just regular output text with no special blocks."
        insights, remaining = extract_insight_blocks(text)
        assert insights == []
        assert remaining == text

    def test_extract_multiple_insight_blocks(self):
        """Two insight blocks in one text chunk are both extracted."""
        from amplifier_module_hooks_streaming_ui.formatting import extract_insight_blocks

        text = (
            "`★ Insight ─────────────────────────────────`\n"
            "First insight\n"
            "`─────────────────────────────────────────────`\n"
            "Middle text\n"
            "`★ Insight ─────────────────────────────────`\n"
            "Second insight\n"
            "`─────────────────────────────────────────────`"
        )
        insights, remaining = extract_insight_blocks(text)
        assert len(insights) == 2
        assert "First insight" in insights[0]
        assert "Second insight" in insights[1]
        assert "Middle text" in remaining

    @pytest.mark.asyncio
    async def test_content_block_end_renders_insight(self):
        """Handler detects insight in text block, renders it, and returns modify."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(insight_mode="explanatory")

        text = (
            "Before.\n\n"
            "`★ Insight ─────────────────────────────────`\n"
            "Important insight content\n"
            "`─────────────────────────────────────────────`\n\n"
            "After."
        )
        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "text", "text": text},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "modify"
        # The modified data should have remaining text without insight block
        modified_block = result.data["block"]
        assert "Important insight content" not in modified_block["text"]
        assert "Before." in modified_block["text"]
        assert "After." in modified_block["text"]

        output = _get_output(buf)
        assert "Insight" in output
        assert "Important insight content" in output

    @pytest.mark.asyncio
    async def test_content_block_end_ignores_when_mode_off(self):
        """insight_mode='off' skips detection entirely."""
        _console, buf = _capture_console()
        hooks = _make_hooks_with_session(insight_mode="off")

        text = (
            "`★ Insight ─────────────────────────────────`\n"
            "Should not be detected\n"
            "`─────────────────────────────────────────────`"
        )
        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "text", "text": text},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        # No rich rendering — raw text would pass through the stream
        assert "Should not be detected" not in output

    @pytest.mark.asyncio
    async def test_content_block_end_passthrough_no_insight(self):
        """Regular text block returns continue when no insight markers present."""
        hooks = _make_hooks_with_session(insight_mode="explanatory")

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "text", "text": "Plain output text."},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"


# ---------------------------------------------------------------------------
# OutputGuard Tests
# ---------------------------------------------------------------------------


class TestOutputGuard:
    """Test OutputGuard spinner-aware lock."""

    def test_pauses_active_spinner_on_enter(self):
        """OutputGuard pauses an active spinner on enter and restarts on exit."""
        from amplifier_module_hooks_streaming_ui.spinner import OutputGuard, SpinnerManager

        mgr = SpinnerManager()
        mgr.start("Working...", depth=1)
        assert mgr.is_active

        guard = OutputGuard(mgr)
        with guard:
            # Spinner should be paused inside the guard
            assert not mgr.is_active
        # Spinner should be restarted after exiting
        assert mgr.is_active
        mgr.stop()

    def test_noop_when_spinner_is_none(self):
        """OutputGuard works as a plain lock when spinner is None."""
        from amplifier_module_hooks_streaming_ui.spinner import OutputGuard

        guard = OutputGuard(None)
        with guard:
            pass  # Should not raise

    def test_noop_when_spinner_not_active(self):
        """OutputGuard is a no-op when spinner exists but isn't running."""
        from amplifier_module_hooks_streaming_ui.spinner import OutputGuard, SpinnerManager

        mgr = SpinnerManager()
        guard = OutputGuard(mgr)
        with guard:
            assert not mgr.is_active

    def test_sequential_with_blocks_no_leak(self):
        """Sequential with blocks don't leak spinner state."""
        from amplifier_module_hooks_streaming_ui.spinner import OutputGuard, SpinnerManager

        mgr = SpinnerManager()
        mgr.start("Task 1", depth=0)
        guard = OutputGuard(mgr)

        with guard:
            assert not mgr.is_active
        assert mgr.is_active

        with guard:
            assert not mgr.is_active
        assert mgr.is_active
        mgr.stop()


# ---------------------------------------------------------------------------
# Spinner Pause/Resume Tests
# ---------------------------------------------------------------------------


class TestSpinnerPauseResume:
    """Test SpinnerManager pause() and resume() methods."""

    def test_pause_returns_state_when_active(self):
        """pause() returns (message, depth) when spinner is active."""
        from amplifier_module_hooks_streaming_ui.spinner import SpinnerManager

        mgr = SpinnerManager()
        mgr.start("Thinking...", depth=2)
        state = mgr.pause()

        assert state is not None
        assert state == ("Thinking...", 2)
        assert not mgr.is_active

    def test_pause_returns_none_when_inactive(self):
        """pause() returns None when no spinner is running."""
        from amplifier_module_hooks_streaming_ui.spinner import SpinnerManager

        mgr = SpinnerManager()
        state = mgr.pause()
        assert state is None

    def test_resume_restarts_with_saved_state(self):
        """resume() restarts spinner with the saved message and depth."""
        from amplifier_module_hooks_streaming_ui.spinner import SpinnerManager

        mgr = SpinnerManager()
        mgr.start("Building...", depth=1)
        state = mgr.pause()
        assert not mgr.is_active

        mgr.resume(state)
        assert mgr.is_active
        mgr.stop()


# ---------------------------------------------------------------------------
# Sub-Agent Text Suppression Tests
# ---------------------------------------------------------------------------


class TestSubAgentTextSuppression:
    """Test that verbose text blocks from sub-agents are suppressed."""

    @pytest.mark.asyncio
    async def test_text_from_depth_gt_zero_suppressed(self):
        """Text blocks from depth > 0 return continue without printing."""
        _console, buf = _capture_console()
        hooks = _make_hooks()
        # Create parent and child sessions
        hooks.state_manager.get_or_create("parent", parent_id=None)
        hooks.state_manager.get_or_create("child", parent_id="parent")

        data = {
            "session_id": "child",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "text", "text": "Verbose sub-agent output here."},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        output = _get_output(buf)
        assert "Verbose sub-agent output" not in output

    @pytest.mark.asyncio
    async def test_text_from_depth_zero_not_suppressed(self):
        """Text blocks from root session (depth=0) are NOT suppressed."""
        hooks = _make_hooks_with_session()

        data = {
            "session_id": "test-session",
            "block_index": 0,
            "total_blocks": 1,
            "block": {"type": "text", "text": "Root session text."},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
