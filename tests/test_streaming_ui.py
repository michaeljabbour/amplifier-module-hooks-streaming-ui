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
        "show_status_bar": False,  # disable status bar thread in tests
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
        hooks = _make_hooks_with_session()
        data = {"block_type": "thinking", "block_index": 0, "session_id": "test-session"}

        result = await hooks.handle_content_block_start("content_block:start", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert 0 in hooks.thinking_blocks

    @pytest.mark.asyncio
    async def test_thinking_block_disabled(self):
        """Test thinking blocks are not tracked when disabled."""
        hooks = _make_hooks(show_thinking=False)
        data = {"block_type": "thinking", "block_index": 0}

        result = await hooks.handle_content_block_start("content_block:start", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert 0 not in hooks.thinking_blocks

    @pytest.mark.asyncio
    async def test_thinking_block_end(self):
        """Test thinking block display on end."""
        _console, buf = _capture_console()
        hooks = _make_hooks()
        hooks.thinking_blocks[0] = {"started": True}

        data = {
            "block_index": 0,
            "block": {"type": "thinking", "thinking": "This is a test thought process."},
        }

        result = await hooks.handle_content_block_end("content_block:end", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert 0 not in hooks.thinking_blocks

        output = _get_output(buf)
        assert "Thinking:" in output
        assert "This is a test thought process." in output

    @pytest.mark.asyncio
    async def test_reasoning_block_end(self):
        """Reasoning blocks should be treated like thinking blocks."""
        _console, buf = _capture_console()
        hooks = _make_hooks()
        hooks.thinking_blocks[1] = {"started": True}

        data = {
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
        assert 1 not in hooks.thinking_blocks

        output = _get_output(buf)
        assert "Thinking:" in output
        assert "Summary insight" in output or "Detailed chain of thought" in output

    @pytest.mark.asyncio
    async def test_non_thinking_blocks_ignored(self):
        """Test that non-thinking blocks are ignored."""
        hooks = _make_hooks()
        data = {"block_type": "text", "block_index": 0}

        result = await hooks.handle_content_block_start("content_block:start", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"
        assert 0 not in hooks.thinking_blocks


# ---------------------------------------------------------------------------
# Tool Call Tests
# ---------------------------------------------------------------------------


class TestToolCalls:
    """Test tool invocation and result display."""

    @pytest.mark.asyncio
    async def test_tool_pre_displays_smart_header(self):
        """Test tool invocation shows smart formatted header."""
        _console, buf = _capture_console()
        hooks = _make_hooks()

        data = {
            "tool_name": "read_file",
            "tool_input": {"file_path": "/some/path/to/file.txt"},
        }

        result = await hooks.handle_tool_pre("tool:pre", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "Read:" in output
        assert "file.txt" in output

    @pytest.mark.asyncio
    async def test_tool_pre_bash(self):
        """Test bash tool shows command in header."""
        _console, buf = _capture_console()
        hooks = _make_hooks()

        data = {"tool_name": "bash", "tool_input": {"command": "npm test"}}

        result = await hooks.handle_tool_pre("tool:pre", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "Bash:" in output
        assert "npm test" in output

    @pytest.mark.asyncio
    async def test_tool_pre_edit_file(self):
        """Test edit_file tool shows diff stats in header."""
        _console, buf = _capture_console()
        hooks = _make_hooks()

        data = {
            "tool_name": "edit_file",
            "tool_input": {
                "file_path": "src/auth.py",
                "old_string": "old_code()",
                "new_string": "new_code()\nextra_line()",
            },
        }

        await hooks.handle_tool_pre("tool:pre", data)

        output = _get_output(buf)
        assert "Edit:" in output
        assert "auth.py" in output
        assert "+2" in output
        assert "-1" in output

    @pytest.mark.asyncio
    async def test_tool_post_success(self):
        """Test successful tool result display."""
        _console, buf = _capture_console()
        hooks = _make_hooks()

        data = {
            "tool_name": "edit_file",
            "tool_response": {"success": True},
        }

        result = await hooks.handle_tool_post("tool:post", data)

        assert isinstance(result, HookResult)
        assert result.action == "continue"

        output = _get_output(buf)
        assert "(done)" in output

    @pytest.mark.asyncio
    async def test_tool_post_failure(self):
        """Test failed tool result display shows error."""
        _console, buf = _capture_console()
        hooks = _make_hooks()

        data = {
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
        hooks.thinking_blocks[0] = {"started": True, "session_id": "test-session"}

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
        hooks.thinking_blocks[0] = {"started": True, "session_id": "test-session"}

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
        hooks.thinking_blocks[0] = {"started": True, "session_id": "test-session"}

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
        hooks.thinking_blocks[0] = {"started": True, "session_id": "test-session"}

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
