"""Minimal markdown to ANSI converter.

Converts a subset of markdown to ANSI escape codes for terminal display.
No external dependencies - pure regex-based conversion.

Supported syntax:
- **bold** and __bold__
- *italic* and _italic_
- `inline code`
- ```code blocks```
- > blockquotes
- # headers (h1-h3)
- [links](url) - shows text with url in parens
"""

import re

from .terminal import BLUE, BOLD, CYAN, DIM, ITALIC, RESET


def md_to_ansi(text: str) -> str:
    """Convert markdown text to ANSI-styled text.

    Processes inline formatting only. For block-level elements like
    code blocks and tables, use the dedicated functions.
    """
    # Process in order of precedence

    # Code spans first (protect from other formatting)
    code_spans: list[str] = []

    def save_code(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_code, text)

    # Bold (** or __)
    text = re.sub(r"\*\*(.+?)\*\*", f"{BOLD}\\1{RESET}", text)
    text = re.sub(r"__(.+?)__", f"{BOLD}\\1{RESET}", text)

    # Italic (* or _) - be careful not to match inside words
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", f"{ITALIC}\\1{RESET}", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", f"{ITALIC}\\1{RESET}", text)

    # Links [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)", f"{BLUE}\\1{RESET} {DIM}(\\2){RESET}", text
    )

    # Restore code spans with styling
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00CODE{i}\x00", f"{CYAN}`{code}`{RESET}")

    return text


def format_header(text: str, level: int) -> str:
    """Format a markdown header."""
    # Remove the # prefix if present
    text = text.lstrip("#").strip()

    if level == 1:
        return f"\n{BOLD}{text}{RESET}\n{'=' * len(text)}\n"
    elif level == 2:
        return f"\n{BOLD}{text}{RESET}\n{'-' * len(text)}\n"
    else:
        return f"\n{BOLD}{text}{RESET}\n"


def format_blockquote(text: str) -> str:
    """Format a blockquote with a vertical bar."""
    lines = text.split("\n")
    formatted = []
    for line in lines:
        # Remove leading > and space
        content = re.sub(r"^>\s?", "", line)
        formatted.append(f"{DIM}│{RESET} {content}")
    return "\n".join(formatted)


def format_code_block(code: str, language: str = "") -> str:
    """Format a fenced code block."""
    lines = code.split("\n")
    formatted = [f"{DIM}┌─{'─' * 40}{RESET}"]
    for line in lines:
        formatted.append(f"{DIM}│{RESET} {line}")
    formatted.append(f"{DIM}└─{'─' * 40}{RESET}")
    return "\n".join(formatted)


def process_markdown(text: str) -> str:
    """Process full markdown text including block elements.

    Handles:
    - Headers
    - Code blocks
    - Blockquotes
    - Inline formatting
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            language = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            result.append(format_code_block("\n".join(code_lines), language))
            i += 1
            continue

        # Header
        header_match = re.match(r"^(#{1,3})\s+(.+)$", line)
        if header_match:
            level = len(header_match.group(1))
            result.append(format_header(header_match.group(2), level))
            i += 1
            continue

        # Blockquote (may span multiple lines)
        if line.startswith(">"):
            quote_lines = []
            while i < len(lines) and (
                lines[i].startswith(">") or (lines[i].strip() and quote_lines)
            ):
                if lines[i].startswith(">"):
                    quote_lines.append(lines[i])
                else:
                    break
                i += 1
            result.append(format_blockquote("\n".join(quote_lines)))
            continue

        # Regular line with inline formatting
        result.append(md_to_ansi(line))
        i += 1

    return "\n".join(result)
