"""Lightweight activity spinner for the streaming UI.

Provides a non-blocking animated spinner using carriage return (\\r)
to overwrite the current line on stderr. Does NOT use Rich Live/Status
(which require a persistent render loop incompatible with hooks).

Thread-safe: uses a daemon timer thread that auto-stops when main exits.
Registers an atexit handler to cleanly stop timers before interpreter
shutdown, preventing "Unhandled exception in event loop" errors.
"""

import atexit
import sys
import threading
from typing import Optional


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
_all_managers: list["SpinnerManager"] = []
_all_managers_lock = threading.Lock()


def _atexit_stop_all() -> None:
    """Stop every spinner before the interpreter tears down threading/IO."""
    global _shutting_down  # noqa: PLW0603
    _shutting_down = True
    with _all_managers_lock:
        for mgr in _all_managers:
            try:
                mgr.stop()
            except Exception:
                pass


atexit.register(_atexit_stop_all)


class Spinner:
    """Animated spinner that overwrites the current terminal line.

    Usage::

        spinner = Spinner("Thinking...", depth=1)
        spinner.start()
        # ... long operation ...
        spinner.stop()  # clears the spinner line
    """

    def __init__(self, message: str = "Thinking...", depth: int = 0):
        self.message = message
        self.depth = depth
        self._frame_idx = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._running = False
        self._file = sys.stderr

    def start(self) -> None:
        """Start the spinner animation."""
        if _shutting_down:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
            self._frame_idx = 0
        self._tick()

    def stop(self, clear: bool = True) -> None:
        """Stop the spinner and optionally clear its line."""
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None

        if clear:
            # Clear the spinner line with carriage return + erase-to-end
            try:
                f = self._file
                if f is not None and not f.closed:
                    f.write("\r\033[K")
                    f.flush()
            except (OSError, ValueError, RuntimeError, TypeError):
                pass

    def update_message(self, message: str) -> None:
        """Update the spinner message while running."""
        with self._lock:
            self.message = message

    def _tick(self) -> None:
        """Render one spinner frame and schedule the next."""
        if _shutting_down:
            return

        with self._lock:
            if not self._running:
                return
            frame = SPINNER_FRAMES[self._frame_idx % len(SPINNER_FRAMES)]
            self._frame_idx += 1
            msg = self.message

        # Plain-text prefix (no Rich markup -- we write directly to stderr)
        prefix_plain = "\u2502 " * self.depth
        line = f"\r{prefix_plain}{frame} {msg}"

        try:
            f = self._file
            if f is None or f.closed:
                return
            f.write(line)
            f.flush()
        except (OSError, ValueError, RuntimeError, TypeError):
            # stderr closed/torn-down during shutdown -- silently bail
            return

        if _shutting_down:
            return

        with self._lock:
            if self._running:
                self._timer = threading.Timer(SPINNER_INTERVAL, self._tick)
                self._timer.daemon = True
                self._timer.start()


class SpinnerManager:
    """Manages spinner lifecycle for the streaming UI hooks.

    Ensures only one spinner is active at a time. Thread-safe.
    Automatically registered for atexit cleanup.
    """

    def __init__(self):
        self._current: Optional[Spinner] = None
        self._lock = threading.Lock()
        # Register for atexit cleanup
        with _all_managers_lock:
            _all_managers.append(self)

    def start(self, message: str = "Thinking...", depth: int = 0) -> None:
        """Start a spinner (stops any existing one first)."""
        if _shutting_down:
            return
        with self._lock:
            if self._current:
                self._current.stop(clear=True)
            self._current = Spinner(message, depth)
            self._current.start()

    def stop(self) -> None:
        """Stop the current spinner."""
        with self._lock:
            if self._current:
                self._current.stop(clear=True)
                self._current = None

    def shutdown(self) -> None:
        """Permanently stop the spinner and prevent future starts."""
        self.stop()

    def update(self, message: str) -> None:
        """Update the current spinner's message."""
        with self._lock:
            if self._current:
                self._current.update_message(message)

    def pause(self) -> tuple[str, int] | None:
        """Pause spinner, clear line. Returns (message, depth) for resume."""
        with self._lock:
            if self._current and self._current._running:
                msg, depth = self._current.message, self._current.depth
                self._current.stop(clear=True)
                self._current = None
                return (msg, depth)
            return None

    def resume(self, state: tuple[str, int]) -> None:
        """Restart a previously paused spinner."""
        self.start(state[0], state[1])

    @property
    def is_active(self) -> bool:
        """Check if a spinner is currently running."""
        with self._lock:
            return self._current is not None and self._current._running


class OutputGuard:
    """Context manager that pauses spinner during Rich Console output.

    Drop-in replacement for threading.Lock() as a context manager.
    """

    def __init__(self, spinner_manager: SpinnerManager | None):
        self._spinner = spinner_manager
        self._lock = threading.Lock()
        self._saved: tuple[str, int] | None = None

    def __enter__(self):
        self._lock.acquire()
        if self._spinner:
            self._saved = self._spinner.pause()
        return self

    def __exit__(self, *exc):
        try:
            if self._saved is not None and self._spinner:
                self._spinner.resume(self._saved)
                self._saved = None
        finally:
            self._lock.release()
        return False
