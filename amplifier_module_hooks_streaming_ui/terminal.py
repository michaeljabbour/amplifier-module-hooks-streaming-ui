"""Terminal output utilities with ANSI escape codes.

Provides low-level terminal control without external dependencies:
- ANSI color and style codes
- Cursor movement and line clearing
- Scroll region management for sticky status bar
"""

import os
import sys
from typing import TextIO

# ============================================================================
# ANSI Escape Codes
# ============================================================================

# Styles
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDERLINE = "\033[4m"
STRIKE = "\033[9m"
RESET = "\033[0m"

# Colors (foreground)
BLACK = "\033[30m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
GRAY = "\033[90m"

# Cursor control
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
SAVE_CURSOR = "\033[s"
RESTORE_CURSOR = "\033[u"

# Line control
CLEAR_LINE = "\033[2K"
CLEAR_TO_END = "\033[K"
MOVE_UP = "\033[A"
MOVE_DOWN = "\033[B"
MOVE_TO_COL_1 = "\r"


# ============================================================================
# Terminal Output Manager
# ============================================================================


class Terminal:
    """Manages terminal output with scroll region support for status bar."""

    def __init__(self, output: TextIO = sys.stdout, error: TextIO = sys.stderr):
        self.output = output
        self.error = error
        self._scroll_region_active = False
        self._status_line: str = ""
        self._original_rows: int = 0

    def get_size(self) -> tuple[int, int]:
        """Get terminal size (columns, rows)."""
        try:
            size = os.get_terminal_size()
            return size.columns, size.lines
        except OSError:
            return 80, 24  # Fallback for non-TTY

    def setup_scroll_region(self) -> None:
        """Reserve bottom line for status bar by setting scroll region."""
        if self._scroll_region_active:
            return

        cols, rows = self.get_size()
        self._original_rows = rows

        # Set scroll region to all but last line
        self.output.write(f"\033[1;{rows - 1}r")
        # Move cursor to top of scroll region
        self.output.write("\033[1;1H")
        self.output.flush()
        self._scroll_region_active = True

    def teardown_scroll_region(self) -> None:
        """Restore normal terminal behavior."""
        if not self._scroll_region_active:
            return

        # Reset scroll region to full terminal
        self.output.write("\033[r")
        # Clear status line
        cols, rows = self.get_size()
        self.output.write(f"\033[{rows};1H{CLEAR_LINE}")
        # Move to bottom
        self.output.write(f"\033[{rows};1H")
        self.output.flush()
        self._scroll_region_active = False
        self._status_line = ""

    def update_status(self, message: str) -> None:
        """Update the status bar at bottom of terminal."""
        cols, rows = self.get_size()

        # Truncate message to terminal width
        if len(message) > cols - 1:
            message = message[: cols - 4] + "..."

        # Save cursor, move to status line, clear it, write message, restore cursor
        self.output.write(
            f"{SAVE_CURSOR}\033[{rows};1H{CLEAR_LINE}{message}{RESTORE_CURSOR}"
        )
        self.output.flush()
        self._status_line = message

    def clear_status(self) -> None:
        """Clear the status bar."""
        if self._status_line:
            cols, rows = self.get_size()
            self.output.write(
                f"{SAVE_CURSOR}\033[{rows};1H{CLEAR_LINE}{RESTORE_CURSOR}"
            )
            self.output.flush()
            self._status_line = ""

    def write(self, text: str) -> None:
        """Write text to output."""
        self.output.write(text)
        self.output.flush()

    def writeln(self, text: str = "") -> None:
        """Write text with newline."""
        self.output.write(text + "\n")
        self.output.flush()

    def hide_cursor(self) -> None:
        """Hide the cursor."""
        self.output.write(HIDE_CURSOR)
        self.output.flush()

    def show_cursor(self) -> None:
        """Show the cursor."""
        self.output.write(SHOW_CURSOR)
        self.output.flush()


# ============================================================================
# Convenience Functions
# ============================================================================


def style(text: str, *styles: str) -> str:
    """Apply ANSI styles to text.

    Example:
        style("hello", BOLD, RED)  # Bold red text
    """
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
