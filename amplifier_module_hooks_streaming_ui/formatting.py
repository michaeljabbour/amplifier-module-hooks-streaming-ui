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


def shorten_paths(text: str, cwd: Path | None = None) -> str:
    """Shorten absolute paths in text: cwd → relative, home → ~.

    Applied to bash commands, grep paths, delegate instructions, etc.
    to keep tool headers compact and readable.
    """
    import os

    if cwd:
        cwd_str = str(cwd)
        # Replace cwd prefix with nothing (makes paths relative)
        text = text.replace(cwd_str + "/", "")
        # Bare cwd reference becomes "."
        text = text.replace(cwd_str, ".")
    home = os.path.expanduser("~")
    if home != "~":
        text = text.replace(home + "/", "~/")
        text = text.replace(home, "~")
    return text


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
        cmd = shorten_paths(cmd, cwd)
        truncated = cmd[:MAX_CMD_LEN] + ("..." if len(cmd) > MAX_CMD_LEN else "")
        return f"Bash: {truncated}"

    if key == "glob":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path")
        if path and path != ".":
            short = shorten_paths(make_relative(path, cwd), cwd)
            return f"Glob: {pattern} in {short}"
        return f"Glob: {pattern}"

    if key == "grep":
        pattern = tool_input.get("pattern", "?")
        path = tool_input.get("path")
        if path and path != ".":
            short = shorten_paths(make_relative(path, cwd), cwd)
            return f'Grep: "{pattern}" in {short}'
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
            instr = shorten_paths(instruction, cwd)
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


def format_result_summary(name: str, result: Any, is_error: bool = False) -> str:
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
        return f"({lines}L)"

    if key in ("bash", "shell"):
        stripped = content.strip()
        if not stripped:
            return "(\u2205)"
        lines = stripped.split("\n")
        return f"({len(lines)}L)"

    if key == "grep":
        stripped = content.strip()
        if not stripped or "no matches" in stripped.lower():
            return "(0)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)} hits)"

    if key == "glob":
        stripped = content.strip()
        if not stripped:
            return "(0)"
        lines = [line for line in stripped.split("\n") if line.strip()]
        return f"({len(lines)})"

    if key in ("write_file", "edit_file"):
        return "(\u2713)"

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


# ============================================================================
# Insight Block Extraction
# ============================================================================

# Quick-check: does the text contain an insight opening delimiter?
INSIGHT_OPEN_PATTERN = re.compile(r"★ Insight")

# Full extraction: opening delimiter line → content → closing delimiter line.
# Uses [─]{10,} to tolerate variable-length dash runs from LLMs.
INSIGHT_BLOCK_PATTERN = re.compile(
    r"`[★]?\s*★ Insight\s*[─]{10,}`\n(.+?)\n`[─]{10,}`",
    re.DOTALL,
)


def extract_insight_blocks(text: str) -> tuple[list[str], str]:
    """Extract insight blocks from text, returning (insights, remaining_text).

    Each insight is the body text between the delimiters.
    remaining_text is the original text with all insight blocks removed.
    """
    insights: list[str] = []
    remaining = text

    for match in INSIGHT_BLOCK_PATTERN.finditer(text):
        insights.append(match.group(1).strip())

    if insights:
        remaining = INSIGHT_BLOCK_PATTERN.sub("", text).strip()

    return insights, remaining


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


class DiffLine:
    """A single line in a code change diff."""

    __slots__ = ("number", "marker", "text")

    def __init__(self, number: int, marker: str, text: str):
        self.number = number  # 0 = skip marker
        self.marker = marker  # " ", "-", "+", "~"
        self.text = text


class CodeChange:
    """Result of format_code_change -- structured diff data for rendering."""

    __slots__ = ("display_path", "summary", "additions", "deletions", "diff_lines")

    def __init__(
        self,
        display_path: str,
        summary: str,
        additions: int,
        deletions: int,
        diff_lines: list["DiffLine"],
    ):
        self.display_path = display_path
        self.summary = summary
        self.additions = additions
        self.deletions = deletions
        self.diff_lines = diff_lines


def format_code_change(
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 3,
    cwd: "Path | None" = None,
) -> CodeChange:
    """Generate a Claude-style inline diff for an edit_file operation.

    Returns a CodeChange with:
    - display_path: shortened file path
    - summary: "Added N lines, removed M lines"
    - diff_lines: list of DiffLine(number, marker, text) for rendering

    Markers: " " context, "-" removed, "+" added, "~" skip.
    """
    rel_path = make_relative(file_path, cwd)

    old_lines = old_string.splitlines() if old_string else []
    new_lines = new_string.splitlines() if new_string else []

    additions, deletions = count_diff_changes(old_string, new_string)

    # Build summary
    parts: list[str] = []
    if additions:
        parts.append(f"Added {additions} line{'s' if additions != 1 else ''}")
    if deletions:
        parts.append(f"removed {deletions} line{'s' if deletions != 1 else ''}")
    summary = ", ".join(parts) if parts else "No changes"

    # Generate diff lines with context using SequenceMatcher
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    diff_lines: list[DiffLine] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            lines = old_lines[i1:i2]
            if len(lines) <= context_lines * 2:
                for idx, line in enumerate(lines):
                    diff_lines.append(DiffLine(i1 + idx + 1, " ", line))
            else:
                for idx in range(context_lines):
                    diff_lines.append(DiffLine(i1 + idx + 1, " ", lines[idx]))
                skipped = len(lines) - context_lines * 2
                if skipped > 0:
                    diff_lines.append(
                        DiffLine(0, "~", f"... {skipped} unchanged lines ...")
                    )
                for idx in range(context_lines):
                    real_idx = len(lines) - context_lines + idx
                    diff_lines.append(DiffLine(i1 + real_idx + 1, " ", lines[real_idx]))
        elif tag == "delete":
            for idx, line in enumerate(old_lines[i1:i2]):
                diff_lines.append(DiffLine(i1 + idx + 1, "-", line))
        elif tag == "insert":
            for idx, line in enumerate(new_lines[j1:j2]):
                diff_lines.append(DiffLine(j1 + idx + 1, "+", line))
        elif tag == "replace":
            for idx, line in enumerate(old_lines[i1:i2]):
                diff_lines.append(DiffLine(i1 + idx + 1, "-", line))
            for idx, line in enumerate(new_lines[j1:j2]):
                diff_lines.append(DiffLine(j1 + idx + 1, "+", line))

    return CodeChange(
        display_path=rel_path,
        summary=summary,
        additions=additions,
        deletions=deletions,
        diff_lines=diff_lines,
    )


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
