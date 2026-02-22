"""Tool formatting and output extraction utilities.

Pure logic layer -- no rendering dependencies, no ANSI, no Rich.
Returns plain strings that the rendering layer can style.

Ported from claudechic/formatting.py, adapted for Amplifier tool names.
"""

import difflib
import json
import re
from pathlib import Path
from typing import Any

# ============================================================================
# Constants
# ============================================================================

MAX_HEADER_WIDTH = 70
MAX_CMD_LEN = 60
MAX_URL_LEN = 50
MAX_TOOL_INPUT_CHARS = 200
MAX_DIFF_PREVIEW = 300


# ============================================================================
# Path Utilities
# ============================================================================


def make_relative(path: str, cwd: Path | None = None) -> str:
    """Make path relative to cwd if possible, otherwise return as-is."""
    if not cwd or not path:
        return path
    try:
        p = Path(path)
        if p.is_absolute() and p.is_relative_to(cwd):
            return str(p.relative_to(cwd))
    except (ValueError, OSError):
        pass
    return path


def truncate_path(path: str, max_len: int = MAX_HEADER_WIDTH) -> str:
    """Truncate path from the front, preserving the tail which is more useful.

    Cuts at a path separator when possible for cleaner output.
    """
    if len(path) <= max_len:
        return path
    available = max_len - 3
    if available <= 0:
        return "..." + path[-max_len:] if max_len > 0 else ""
    suffix = path[-available:]
    sep_idx = suffix.find("/")
    if 0 < sep_idx < len(suffix) - 1:
        suffix = suffix[sep_idx:]
    return "..." + suffix


# ============================================================================
# Tool Header Formatting
# ============================================================================


def format_tool_header(
    name: str, tool_input: dict[str, Any], cwd: Path | None = None
) -> str:
    """Format a concise one-line header for a tool invocation.

    Transforms generic tool names into human-readable summaries:
        edit_file  -> "Edit: src/auth.py (+3, -1)"
        bash       -> "Bash: npm test"
        delegate   -> "Task: Survey auth/ (foundation:explorer)"
    """
    key = name.lower()

    if key == "edit_file":
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        additions, deletions = count_diff_changes(old, new)
        stats = f" (+{additions}, -{deletions})"
        path = make_relative(tool_input.get("file_path", "?"), cwd)
        path = truncate_path(path, MAX_HEADER_WIDTH - 6 - len(stats))
        return f"Edit: {path}{stats}"

    if key == "write_file":
        path = make_relative(tool_input.get("file_path", "?"), cwd)
        path = truncate_path(path, MAX_HEADER_WIDTH - 7)
        return f"Write: {path}"

    if key == "read_file":
        path = make_relative(tool_input.get("file_path", "?"), cwd)
        path = truncate_path(path, MAX_HEADER_WIDTH - 6)
        extra = ""
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        if isinstance(offset, int) or isinstance(limit, int):
            start = offset if isinstance(offset, int) else 1
            if isinstance(limit, int):
                extra = f" (lines {start}-{start + limit})"
            else:
                extra = f" (from line {start})"
        return f"Read: {path}{extra}"

    if key in ("bash", "shell"):
        cmd = tool_input.get("command", "?")
        description = tool_input.get("description", "")
        if description:
            return f"Bash: {description}"
        truncated = cmd[:MAX_CMD_LEN] + ("..." if len(cmd) > MAX_CMD_LEN else "")
        return f"Bash: {truncated}"

    if key == "glob":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path")
        if path and path != ".":
            rel_path = make_relative(path, cwd)
            # Also shorten home dir
            import os
            home = os.path.expanduser("~")
            rel_path = rel_path.replace(home + "/", "~/").replace(home, "~")
            return f"Glob: {pattern} in {rel_path}"
        return f"Glob: {pattern}"

    if key == "grep":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path")
        if path and path != ".":
            return f'Grep: "{pattern}" in {path}'
        return f'Grep: "{pattern}"'

    if key == "web_search":
        query = tool_input.get("query", "?")
        return f"Search: {query}"

    if key == "web_fetch":
        url = tool_input.get("url", "?")
        truncated = url[:MAX_URL_LEN] + ("..." if len(url) > MAX_URL_LEN else "")
        return f"Fetch: {truncated}"

    if key == "delegate":
        agent = tool_input.get("agent", "")
        instruction = tool_input.get("instruction", "")
        short_agent = agent.split(":")[-1] if ":" in agent else agent
        if instruction:
            # Relativize any absolute paths in the instruction
            instr = instruction
            if cwd:
                cwd_str = str(cwd)
                instr = instr.replace(cwd_str + "/", "./").replace(cwd_str, ".")
            # Also shorten home directory
            import os
            home = os.path.expanduser("~")
            instr = instr.replace(home + "/", "~/").replace(home, "~")
            instr_preview = instr[:50] + ("..." if len(instr) > 50 else "")
            return f"Task: {instr_preview} ({short_agent})"
        return f"Task: {short_agent}"

    if key == "todo":
        action = tool_input.get("action", "")
        todos = tool_input.get("todos", [])
        if action in ("create", "update"):
            return f"Todo: {action} {len(todos)} items"
        return f"Todo: {action}"

    if key == "recipes":
        operation = tool_input.get("operation", "")
        recipe_path = tool_input.get("recipe_path", "")
        if recipe_path:
            recipe_name = Path(recipe_path).stem
            return f"Recipe: {operation} {recipe_name}"
        return f"Recipe: {operation}"

    if key == "python_check":
        paths = tool_input.get("paths", [])
        if paths:
            return f"PythonCheck: {', '.join(str(p) for p in paths[:3])}"
        content = tool_input.get("content")
        if content:
            return "PythonCheck: (inline code)"
        return "PythonCheck"

    if key == "lsp":
        operation = tool_input.get("operation", "?")
        file_path = tool_input.get("file_path", "")
        line = tool_input.get("line", "")
        short_path = Path(file_path).name if file_path else ""
        if line:
            return f"LSP: {operation} {short_path}:{line}"
        return f"LSP: {operation} {short_path}"

    if key == "load_skill":
        skill_name = tool_input.get("skill_name", "")
        if skill_name:
            return f"Skill: {skill_name}"
        if tool_input.get("list"):
            return "Skill: list"
        search = tool_input.get("search", "")
        if search:
            return f'Skill: search "{search}"'
        return "Skill"

    # Fallback for unknown tools
    return name


# ============================================================================
# Result Summary
# ============================================================================


def format_result_summary(
    name: str, result: Any, is_error: bool = False
) -> str:
    """Extract a short parenthesized summary from a tool result.

    Returns strings like "(143 lines)", "(done)", "(error)", "(3 matches)".
    """
    if is_error:
        return "(error)"

    content = extract_output(result)
    key = name.lower()

    if key == "read_file":
        if not content.strip():
            return "(empty)"
        lines = content.count("\n") + 1
        return f"({lines} lines)"

    if key in ("bash", "shell"):
        stripped = content.strip()
        if not stripped:
            return "(no output)"
        lines = stripped.split("\n")
        return f"({len(lines)} lines)"

    if key == "grep":
        stripped = content.strip()
        if not stripped or "no matches" in stripped.lower():
            return "(no matches)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)} matches)"

    if key == "glob":
        stripped = content.strip()
        if not stripped:
            return "(no files)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)} files)"

    if key in ("write_file", "edit_file"):
        return "(done)"

    if key == "delegate":
        if not content.strip():
            return "(complete)"
        lines = content.strip().split("\n")
        return f"({len(lines)} lines)"

    if key == "web_search":
        if not content.strip():
            return "(no results)"
        return ""

    if key == "todo":
        return "(done)"

    if key == "python_check":
        if isinstance(result, dict):
            if result.get("clean"):
                return "(clean)"
            if result.get("success"):
                return "(ok)"
            return "(issues found)"
        return ""

    return ""


# ============================================================================
# Tool Input Formatting (expanded detail view)
# ============================================================================


def format_tool_input(
    name: str, tool_input: dict[str, Any], cwd: Path | None = None
) -> str:
    """Format tool input as a multi-line plain-text block for expanded display.

    This is the detailed view, NOT the one-line header (that's format_tool_header).
    """
    key = name.lower()

    if key == "write_file":
        content = tool_input.get("content", "")
        preview = content[:400] + ("..." if len(content) > 400 else "")
        return preview

    if key == "read_file":
        path = make_relative(tool_input.get("file_path", "?"), cwd)
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        if isinstance(offset, int) or isinstance(limit, int):
            start = offset if isinstance(offset, int) else 1
            end = f"{start + limit}" if isinstance(limit, int) else "end"
            return f"{path} (lines {start}-{end})"
        return path

    if key in ("bash", "shell"):
        return tool_input.get("command", "?")

    if key == "glob":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path")
        if path and path != ".":
            return f"{pattern} in {path}"
        return pattern

    if key == "grep":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path")
        if path and path != ".":
            return f"{pattern} in {path}"
        return pattern

    if key == "edit_file":
        path = make_relative(tool_input.get("file_path", "?"), cwd)
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        diff = format_diff_text(old, new)
        return f"{path}\n{diff}"

    if key == "delegate":
        agent = tool_input.get("agent", "")
        instruction = tool_input.get("instruction", "")
        parts = []
        if agent:
            parts.append(f"Agent: {agent}")
        if instruction:
            parts.append(f"Instruction: {instruction}")
        return "\n".join(parts)

    # Fallback: JSON dump
    try:
        return json.dumps(tool_input, indent=2, default=str)
    except Exception:
        return str(tool_input)


# ============================================================================
# Diff Utilities
# ============================================================================


def count_diff_changes(old: str, new: str) -> tuple[int, int]:
    """Count additions and deletions between two strings.

    Returns (additions, deletions) as line counts.
    """
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)

    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            deletions += i2 - i1
        elif tag == "insert":
            additions += j2 - j1
        elif tag == "replace":
            deletions += i2 - i1
            additions += j2 - j1
    return additions, deletions


def format_diff_text(old: str, new: str, max_len: int = MAX_DIFF_PREVIEW) -> str:
    """Format a unified-style diff as plain text with +/- prefixes.

    The rendering layer can add color (red for -, green for +) on top.
    """
    old_preview = old[:max_len] + ("..." if len(old) > max_len else "")
    new_preview = new[:max_len] + ("..." if len(new) > max_len else "")
    old_lines = old_preview.splitlines() if old_preview else []
    new_lines = new_preview.splitlines() if new_preview else []

    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    result_lines: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in old_lines[i1:i2]:
                result_lines.append(f"  {line}")
        elif tag == "delete":
            for line in old_lines[i1:i2]:
                result_lines.append(f"- {line}")
        elif tag == "insert":
            for line in new_lines[j1:j2]:
                result_lines.append(f"+ {line}")
        elif tag == "replace":
            for line in old_lines[i1:i2]:
                result_lines.append(f"- {line}")
            for line in new_lines[j1:j2]:
                result_lines.append(f"+ {line}")
    return "\n".join(result_lines)


# ============================================================================
# Output Extraction
# ============================================================================


def extract_output(result: Any) -> str:
    """Extract displayable text from a tool result.

    Handles the various result shapes returned by Amplifier tools:
    - dict with stdout/stderr (bash)
    - dict with output/content fields
    - plain strings
    - lists
    - None
    """
    if result is None:
        return ""

    if isinstance(result, str):
        return result

    if isinstance(result, dict):
        # Bash-style results
        if "stdout" in result or "stderr" in result:
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            returncode = result.get("returncode", 0)
            if returncode == 0:
                return stdout or stderr or ""
            output = stdout
            if stderr:
                output = (
                    f"{output}\n[stderr]: {stderr}" if output else f"[stderr]: {stderr}"
                )
            return output or ""

        # Common wrapper fields
        if "output" in result:
            return extract_output(result["output"])
        if "content" in result:
            return extract_output(result["content"])

        # Fallback: JSON
        try:
            return json.dumps(result, indent=2, default=str)
        except Exception:
            return str(result)

    if isinstance(result, list):
        if not result:
            return ""
        return "\n".join(str(item) for item in result[:20])

    return str(result)


# ============================================================================
# Misc Utilities
# ============================================================================

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".css": "css",
    ".html": "html",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sh": "bash",
    ".bash": "bash",
}


def get_lang_from_path(path: str) -> str:
    """Guess language from file extension for syntax highlighting."""
    ext = Path(path).suffix.lower()
    return _EXT_TO_LANG.get(ext, "")


def is_error_result(result: Any) -> bool:
    """Determine if a tool result represents an error."""
    if result is None:
        return False
    if isinstance(result, dict):
        if "returncode" in result:
            return result.get("returncode", 0) != 0
        if "success" in result:
            return not result.get("success", True)
        if "error" in result:
            return True
    return False
