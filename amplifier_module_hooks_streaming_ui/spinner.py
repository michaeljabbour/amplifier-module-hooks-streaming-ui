"""Lightweight activity spinner for the streaming UI.

Provides a non-blocking animated spinner using carriage return (\\r)
to overwrite the current line on stderr. Does NOT use Rich Live/Status
(which require a persistent render loop incompatible with hooks).

Thread-safe: uses a daemon timer thread that auto-stops when main exits.
"""

import sys
import threading
from typing import Optional


# Braille spinner frames (smooth 10-frame animation)
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPINNER_INTERVAL = 0.1  # seconds between frames


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
                self._file.write("\r\033[K")
                self._file.flush()
            except (OSError, ValueError):
                pass

    def update_message(self, message: str) -> None:
        """Update the spinner message while running."""
        with self._lock:
            self.message = message

    def _tick(self) -> None:
        """Render one spinner frame and schedule the next."""
        with self._lock:
            if not self._running:
                return
            frame = SPINNER_FRAMES[self._frame_idx % len(SPINNER_FRAMES)]
            self._frame_idx += 1
            msg = self.message

        # Plain-text prefix (no Rich markup — we write directly to stderr)
        prefix_plain = "│ " * self.depth
        line = f"\r{prefix_plain}{frame} {msg}"

        try:
            self._file.write(line)
            self._file.flush()
        except (OSError, ValueError):
            return

        with self._lock:
            if self._running:
                self._timer = threading.Timer(SPINNER_INTERVAL, self._tick)
                self._timer.daemon = True
                self._timer.start()


class SpinnerManager:
    """Manages spinner lifecycle for the streaming UI hooks.

    Ensures only one spinner is active at a time. Thread-safe.
    """

    def __init__(self):
        self._current: Optional[Spinner] = None
        self._lock = threading.Lock()

    def start(self, message: str = "Thinking...", depth: int = 0) -> None:
        """Start a spinner (stops any existing one first)."""
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

    def update(self, message: str) -> None:
        """Update the current spinner's message."""
        with self._lock:
            if self._current:
                self._current.update_message(message)

    @property
    def is_active(self) -> bool:
        """Check if a spinner is currently running."""
        with self._lock:
            return self._current is not None and self._current._running
