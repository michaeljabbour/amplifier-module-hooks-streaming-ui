"""Markdown table detection and formatting.

Detects markdown tables in text and formats them with proper alignment
and ANSI styling for terminal display.
"""

import re
from typing import Optional

from .terminal import BOLD, DIM, RESET, visible_len


def is_table_separator(line: str) -> bool:
    """Check if line is a table separator (|---|---|)."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    # Check for separator pattern: | :?-+:? | ...
    return bool(re.match(r"^\|[\s:]*-+[\s:]*\|", stripped))


def parse_table_row(line: str) -> list[str]:
    """Parse a table row into cells."""
    # Remove leading/trailing pipes and split
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def detect_table(
    lines: list[str], start_idx: int
) -> Optional[tuple[int, list[list[str]]]]:
    """Detect a markdown table starting at given index.

    Returns (end_idx, rows) if table found, None otherwise.
    A valid table has:
    - Header row with | separators
    - Separator row (|---|---|)
    - One or more data rows
    """
    if start_idx >= len(lines):
        return None

    # Check for header row
    header_line = lines[start_idx]
    if "|" not in header_line:
        return None

    # Check for separator row
    if start_idx + 1 >= len(lines):
        return None

    sep_line = lines[start_idx + 1]
    if not is_table_separator(sep_line):
        return None

    # Parse header
    header = parse_table_row(header_line)
    rows = [header]

    # Parse data rows
    i = start_idx + 2
    while i < len(lines):
        line = lines[i]
        if "|" not in line or line.strip() == "":
            break
        rows.append(parse_table_row(line))
        i += 1

    # Need at least header + separator + one data row
    if len(rows) < 2:
        return None

    return (i, rows)


def format_table(rows: list[list[str]], max_width: int = 80) -> str:
    """Format table rows with proper column widths.

    Args:
        rows: List of rows, each row is a list of cell values
        max_width: Maximum total table width

    Returns:
        Formatted table string with ANSI styling
    """
    if not rows:
        return ""

    # Calculate column widths (using visible length for ANSI)
    num_cols = max(len(row) for row in rows)
    col_widths = [0] * num_cols

    for row in rows:
        for i, cell in enumerate(row):
            if i < num_cols:
                col_widths[i] = max(col_widths[i], visible_len(cell))

    # Cap column widths if total exceeds max_width
    # Account for separators: | col | col | = 3 chars per col + 1
    total_width = sum(col_widths) + (num_cols * 3) + 1
    if total_width > max_width:
        # Reduce widths proportionally
        excess = total_width - max_width
        for i in range(num_cols):
            reduction = int(excess * (col_widths[i] / total_width))
            col_widths[i] = max(5, col_widths[i] - reduction)

    # Build output
    output_lines = []

    for row_idx, row in enumerate(rows):
        cells = []
        for i in range(num_cols):
            cell = row[i] if i < len(row) else ""
            # Truncate if needed
            if visible_len(cell) > col_widths[i]:
                cell = cell[: col_widths[i] - 3] + "..."
            # Pad to width
            padding = col_widths[i] - visible_len(cell)
            cell = cell + " " * padding

            # Style header row
            if row_idx == 0:
                cell = f"{BOLD}{cell}{RESET}"

            cells.append(cell)

        line = "| " + " | ".join(cells) + " |"
        output_lines.append(line)

        # Add separator after header
        if row_idx == 0:
            sep_cells = ["-" * w for w in col_widths]
            sep_line = "|-" + "-|-".join(sep_cells) + "-|"
            output_lines.append(f"{DIM}{sep_line}{RESET}")

    return "\n".join(output_lines)


def process_tables(text: str) -> str:
    """Process text and format any markdown tables found."""
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        table_result = detect_table(lines, i)
        if table_result:
            end_idx, rows = table_result
            result.append(format_table(rows))
            i = end_idx
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)
