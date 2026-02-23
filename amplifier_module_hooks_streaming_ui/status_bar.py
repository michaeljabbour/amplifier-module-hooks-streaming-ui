"""Status bar data provider for the streaming UI.

Exports a thread-safe StatusInfo dataclass that the CLI's prompt_toolkit
bottom_toolbar can read and render. The hooks module updates this on every
event. The CLI polls or subscribes to changes.

Architecture::

    hooks ──update_status()──> StatusInfo (thread-safe singleton)
    CLI   ──get_status()────> reads StatusInfo for toolbar rendering

IMPORTANT: This module does NOT render to the terminal. Direct ANSI cursor
pinning was intentionally avoided because it conflicts with prompt_toolkit
(see terminal.py: "Scroll region / status bar removed - conflicts with
prompt_toolkit REPL").
"""

import threading
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class StatusInfo:
    """Current status for the footer bar."""

    # Phase display
    phase: str = "Ready"
    phase_style: str = "green"  # green=ready, yellow=thinking, cyan=tool, red=error

    # Context breadcrumb (e.g. "main → Explorer → Deep-Scan")
    breadcrumb: str = ""

    # Current tool (when running)
    current_tool: str = ""

    # Token counts
    input_tokens: int = 0
    output_tokens: int = 0
    cache_pct: int = 0

    # Timing
    elapsed: str = ""

    # Model info
    model: str = ""

    # Error (most recent)
    last_error: str = ""

    # Cost
    cost: str = ""


class StatusBarProvider:
    """Thread-safe status info provider.

    The hooks module calls update() on every event.
    The CLI calls get_status() to read current state.
    """

    def __init__(self) -> None:
        self._status = StatusInfo()
        self._lock = threading.Lock()
        self._on_change: Optional[Callable[[], None]] = None

    def get_status(self) -> StatusInfo:
        """Get current status (thread-safe snapshot)."""
        with self._lock:
            return StatusInfo(
                phase=self._status.phase,
                phase_style=self._status.phase_style,
                breadcrumb=self._status.breadcrumb,
                current_tool=self._status.current_tool,
                input_tokens=self._status.input_tokens,
                output_tokens=self._status.output_tokens,
                cache_pct=self._status.cache_pct,
                elapsed=self._status.elapsed,
                model=self._status.model,
                last_error=self._status.last_error,
                cost=self._status.cost,
            )

    def update(self, **kwargs: object) -> None:
        """Update status fields (thread-safe write)."""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._status, key):
                    setattr(self._status, key, value)

        # Notify listener if registered (e.g. prompt_toolkit app.invalidate())
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                pass

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a callback for status changes.

        Typical usage with prompt_toolkit::

            status_bar.on_change(lambda: app.invalidate())
        """
        self._on_change = callback

    def format_stats_line(self) -> str:
        """Format status stats without phase indicator.

        Returns compact stats string for LiveFooter to append to the spinner
        line during execution::

            ↓ 87k (48%) · ↑ 625 │ ⏱ 00:28 │ opus-4-6

        Unlike ``format_toolbar()``, this omits the phase/tool indicator
        (the spinner already conveys that) and the breadcrumb (too wide
        for a single stderr line).
        """
        s = self.get_status()
        parts: list[str] = []

        # Tokens (compact)
        if s.input_tokens > 0:
            inp = _compact_number(s.input_tokens)
            out = _compact_number(s.output_tokens)
            token_str = f"\u2193 {inp}"
            if s.cache_pct > 0:
                token_str += f" ({s.cache_pct}%)"
            token_str += f" \u00b7 \u2191 {out}"
            parts.append(token_str)

        # Elapsed
        if s.elapsed:
            parts.append(f"\u23f1 {s.elapsed}")

        # Cost
        if s.cost:
            parts.append(s.cost)

        # Model
        if s.model:
            short = s.model.replace("claude-", "").replace("-20250514", "")
            parts.append(short)

        return " \u2502 ".join(parts)

    def format_toolbar(self) -> str:
        """Format status as a plain string for prompt_toolkit bottom_toolbar.

        Returns a string like::

            ● Thinking │ main → Explorer │ ↓ 87k · ↑ 625 │ ⏱ 00:28 │ opus-4-6

        The CLI can call this directly in its bottom_toolbar callback,
        or use get_status() for more granular control.
        """
        s = self.get_status()
        parts: list[str] = []

        # Phase indicator
        if s.phase == "Ready":
            parts.append("● Ready")
        elif s.phase == "Done":
            parts.append("✓ Done")
        elif s.phase == "Error":
            parts.append(f"✗ {s.last_error}" if s.last_error else "✗ Error")
        elif s.current_tool:
            parts.append(f"⠋ {s.current_tool}")
        else:
            parts.append(f"⠋ {s.phase}")

        # Breadcrumb
        if s.breadcrumb:
            parts.append(s.breadcrumb)

        # Tokens (compact)
        if s.input_tokens > 0:
            inp = _compact_number(s.input_tokens)
            out = _compact_number(s.output_tokens)
            token_str = f"↓ {inp}"
            if s.cache_pct > 0:
                token_str += f" ({s.cache_pct}%)"
            token_str += f" · ↑ {out}"
            parts.append(token_str)

        # Elapsed
        if s.elapsed:
            parts.append(f"⏱ {s.elapsed}")

        # Cost
        if s.cost:
            parts.append(s.cost)

        # Model
        if s.model:
            short = s.model.replace("claude-", "").replace("-20250514", "")
            parts.append(short)

        return " │ ".join(parts)


def _compact_number(n: int) -> str:
    """Format large numbers compactly: 1234 → '1.2k', 1234567 → '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
