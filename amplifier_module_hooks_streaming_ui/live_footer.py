"""Animated activity footer for the streaming UI.

Combines spinner animation and output serialization into a single class,
replacing the separate Spinner, SpinnerManager, and OutputGuard classes.

Provides an animated braille spinner with a status message that coordinates
cleanly with Rich Console output via the ``output()`` context manager.

Thread-safe: uses a daemon timer thread that auto-stops when main exits.
Registers an atexit handler to prevent interpreter shutdown errors.
"""

from __future__ import annotations

import atexit
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator, Optional

if TYPE_CHECKING:
    from collections.abc import Callable

# Braille spinner frames (smooth 10-frame animation)
SPINNER_FRAMES = [
    "\u280b",
    "\u2819",
    "\u2839",
    "\u2838",
    "\u283c",
    "\u2834",
    "\u2826",
    "\u2827",
    "\u2807",
    "\u280f",
]
SPINNER_INTERVAL = 0.1  # seconds between frames

# Module-level shutdown flag -- once set, no new timers are created.
_shutting_down = False
_all_footers: list["LiveFooter"] = []
_all_footers_lock = threading.Lock()

# Singleton footer for process-wide sharing across sessions.
_singleton_footer: Optional["LiveFooter"] = None
_singleton_lock = threading.Lock()


def _atexit_cleanup() -> None:
    """Stop all footers before interpreter teardown."""
    global _shutting_down  # noqa: PLW0603
    _shutting_down = True
    with _all_footers_lock:
        for footer in _all_footers:
            try:
                footer.shutdown()
            except Exception:
                pass


atexit.register(_atexit_cleanup)


def _reset_singleton() -> None:
    """Reset the singleton for testing. Not for production use."""
    global _singleton_footer  # noqa: PLW0603
    with _singleton_lock:
        if _singleton_footer is not None:
            _singleton_footer.shutdown()
            with _all_footers_lock:
                try:
                    _all_footers.remove(_singleton_footer)
                except ValueError:
                    pass
            _singleton_footer = None


def get_footer(enabled: bool = True) -> "LiveFooter":
    """Get or create the process-wide singleton LiveFooter.

    When parent and child sessions share the same process (e.g. during
    delegate), a single footer prevents concurrent timer threads from
    fighting over stderr and causing spinner-line stacking.
    """
    global _singleton_footer  # noqa: PLW0603
    with _singleton_lock:
        if _singleton_footer is None:
            _singleton_footer = LiveFooter(enabled=enabled)
        elif enabled and not _singleton_footer._enabled:
            # Upgrade: first caller was disabled, new caller wants animation.
            _singleton_footer._enabled = True
        return _singleton_footer


class LiveFooter:
    """Animated status footer with built-in output coordination.

    Shows a braille spinner animation with a status message on stderr.
    Provides an ``output()`` context manager that automatically clears the
    footer line before Rich Console prints and restores it after.

    When *enabled* is ``False``, ``show()`` is a no-op (no background threads
    are ever created), but ``output()`` still serializes concurrent callers.

    Usage::

        footer = LiveFooter()
        footer.show("Thinking...", depth=1)

        with footer.output():
            console.print("some rich output")

        footer.hide()
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._message: str = ""
        self._depth: int = 0
        self._active: bool = False
        self._frame_idx: int = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()  # protects animation state
        self._output_lock = threading.Lock()  # serializes output() blocks
        self._file = sys.stderr
        self._status_fn: Callable[[], str] | None = None

        with _all_footers_lock:
            _all_footers.append(self)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self, message: str, depth: int = 0) -> None:
        """Start or update the animated footer."""
        if not self._enabled or _shutting_down:
            return
        with self._lock:
            self._message = message
            self._depth = depth
            if not self._active:
                self._active = True
                self._frame_idx = 0
                self._schedule_tick()

    def hide(self) -> None:
        """Stop the animation and clear the footer line."""
        with self._lock:
            was_active = self._active
            self._active = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if was_active:
            self._clear_line()

    def update(self, message: str) -> None:
        """Update the footer message while it is showing."""
        with self._lock:
            self._message = message

    def set_status_provider(self, fn: Callable[[], str]) -> None:
        """Set a callable that returns stats text to append to the spinner line.

        The callable should return a short plain-text string (e.g. token counts,
        elapsed time, model name).  It is invoked on every animation tick, so it
        must be fast and thread-safe.
        """
        self._status_fn = fn

    def shutdown(self) -> None:
        """Permanently stop the footer (called during cleanup)."""
        self.hide()

    @contextmanager
    def output(self) -> Generator[None, None, None]:
        """Pause animation and serialize output for the duration of the block.

        Clears the footer line on enter, restores it on exit.  Also acts
        as a mutex so concurrent hook callbacks don't interleave Rich output.
        """
        self._output_lock.acquire()

        # Snapshot and pause
        with self._lock:
            was_active = self._active
            saved_msg = self._message
            saved_depth = self._depth
            if was_active:
                self._active = False
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
        if was_active:
            self._clear_line()

        try:
            yield
        finally:
            if was_active:
                self.show(saved_msg, saved_depth)
            self._output_lock.release()

    @property
    def is_active(self) -> bool:
        """Whether the footer animation is currently running."""
        with self._lock:
            return self._active

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule_tick(self) -> None:
        """Schedule the next animation frame.  Called with ``_lock`` held."""
        if _shutting_down:
            return
        self._timer = threading.Timer(SPINNER_INTERVAL, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self) -> None:
        """Render one spinner frame and schedule the next."""
        if _shutting_down:
            return

        with self._lock:
            if not self._active:
                return
            frame = SPINNER_FRAMES[self._frame_idx % len(SPINNER_FRAMES)]
            self._frame_idx += 1
            msg = self._message
            depth = self._depth
            self._timer = None

        # Enforce single-line: delegate descriptions can contain newlines which
        # cause \r to only rewind the *last* line, leaving prior lines stacked.
        if "\n" in msg:
            msg = msg.split("\n", 1)[0]

        prefix = "\u2502 " * depth

        # Append live stats from the status provider (tokens, elapsed, model)
        status_suffix = ""
        if self._status_fn:
            try:
                stats = self._status_fn()
                if stats:
                    status_suffix = f" \u2502 {stats}"
            except Exception:
                pass

        visible = f"{prefix}{frame} {msg}{status_suffix}"

        # Truncate to terminal width to prevent line wrapping
        try:
            import shutil as _shutil

            cols = _shutil.get_terminal_size().columns
            if len(visible) > cols:
                visible = visible[: cols - 1]
        except Exception:
            pass

        line = f"\r{visible}\033[K"

        # Guard against ghost lines: output() holds _output_lock while it
        # clears the spinner line and prints Rich output.  If we wrote here
        # while that lock is held, the frame would land between the clear
        # and the Rich print, leaving a permanent "ghost" line in scrollback.
        # Non-blocking acquire: skip this frame if output() is active.
        if not self._output_lock.acquire(blocking=False):
            with self._lock:
                if self._active:
                    self._schedule_tick()
            return

        try:
            f = self._file
            if f is not None and not f.closed:
                f.write(line)
                f.flush()
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
        finally:
            self._output_lock.release()

        if _shutting_down:
            return

        with self._lock:
            if self._active:
                self._schedule_tick()

    def _clear_line(self) -> None:
        """Clear the current footer line on stderr."""
        try:
            f = self._file
            if f is not None and not f.closed:
                f.write("\r\033[K")
                f.flush()
        except (OSError, ValueError, RuntimeError, TypeError):
            pass
