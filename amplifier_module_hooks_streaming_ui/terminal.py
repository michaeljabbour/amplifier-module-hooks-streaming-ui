"""Terminal output utilities.

Provides ANSI style helpers and simple write functions.
Scroll region / status bar removed - conflicts with prompt_toolkit REPL.
"""


# Styles
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
RESET = "\033[0m"

# Colors
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GRAY = "\033[90m"


def style(text: str, *styles: str) -> str:
    """Apply ANSI styles to text."""
    if not styles:
        return text
    return "".join(styles) + text + RESET


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    import re

    return re.sub(r"\033\[[0-9;]*m", "", text)


def visible_len(text: str) -> int:
    """Get visible length of text (excluding ANSI codes)."""
    return len(strip_ansi(text))
