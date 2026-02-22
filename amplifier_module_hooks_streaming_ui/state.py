"""Session state management for streaming UI.

Tracks the current phase of execution and accumulates metrics
for display in the UI.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional


class Phase(Enum):
    """Current phase of the session."""

    IDLE = auto()  # Waiting for input
    THINKING = auto()  # Processing, before first token
    STREAMING = auto()  # Receiving assistant text
    TOOL_CALLING = auto()  # Tool call received, about to execute
    TOOL_RUNNING = auto()  # Tool is executing
    COMPLETE = auto()  # Turn complete
    ERROR = auto()  # Error state


# Human-readable state names for status bar
PHASE_DISPLAY = {
    Phase.IDLE: "Ready",
    Phase.THINKING: "Thinking…",
    Phase.STREAMING: "Responding…",
    Phase.TOOL_CALLING: "Calling tool…",
    Phase.TOOL_RUNNING: "Running…",
    Phase.COMPLETE: "Done",
    Phase.ERROR: "Error",
}


@dataclass
class ToolCall:
    """Represents a tool call in progress."""

    name: str
    arguments: dict
    start_time: datetime = field(default_factory=datetime.now)


@dataclass
class SessionMetrics:
    """Accumulated metrics for a session."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    tool_calls: int = 0
    thinking_time: float = 0.0  # seconds spent in thinking blocks

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_create_tokens
        )


@dataclass
class SessionState:
    """State for a single session (root or nested)."""

    session_id: str
    phase: Phase = Phase.IDLE
    start_time: datetime = field(default_factory=datetime.now)

    # Current activity
    current_tool: Optional[ToolCall] = None
    thinking_start: Optional[datetime] = None

    # Metrics
    metrics: SessionMetrics = field(default_factory=SessionMetrics)

    # Nesting
    depth: int = 0
    parent_id: Optional[str] = None

    # Model info for cost calculation
    model: Optional[str] = None
    provider: Optional[str] = None

    # Agent identity (for sub-sessions)
    agent_name: Optional[str] = None
    agent_type: Optional[str] = None
    agent_desc: Optional[str] = None

    # Buffered tool header for compact single-line output
    pending_tool_header: Optional[str] = None

    # Thinking text accumulator (for accordion preview)
    thinking_text: str = ""

    # Pending agent info for delegate tool (transferred to child on task:spawned)
    _pending_agent_info: Optional[dict[str, str]] = None

    def elapsed_seconds(self) -> float:
        """Seconds since session started."""
        return (datetime.now() - self.start_time).total_seconds()

    def elapsed_formatted(self) -> str:
        """Format elapsed time as MM:SS or HH:MM:SS."""
        total = int(self.elapsed_seconds())
        minutes, seconds = divmod(total, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


class StateManager:
    """Manages state across multiple sessions (for nested agents)."""

    def __init__(self):
        self.sessions: dict[str, SessionState] = {}
        self.root_session_id: Optional[str] = None
        self._current_session_id: Optional[str] = None

    def get_or_create(
        self,
        session_id: str,
        parent_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> SessionState:
        """Get existing session or create new one.

        Merges newly-available parent/depth/model/provider into an existing
        state.  This handles the race where ``session:start`` fires before
        ``task:spawned`` — the first call creates the state with depth 0,
        and the second call patches in the correct parent and depth.
        """
        if session_id in self.sessions:
            existing = self.sessions[session_id]
            # Patch depth/parent when we finally learn who the parent is
            if parent_id and not existing.parent_id and parent_id in self.sessions:
                existing.parent_id = parent_id
                existing.depth = self.sessions[parent_id].depth + 1
            if model and not existing.model:
                existing.model = model
            if provider and not existing.provider:
                existing.provider = provider
            return existing

        depth = 0
        if parent_id and parent_id in self.sessions:
            depth = self.sessions[parent_id].depth + 1

        state = SessionState(
            session_id=session_id,
            depth=depth,
            parent_id=parent_id,
            model=model,
            provider=provider,
        )
        self.sessions[session_id] = state

        if parent_id is None:
            self.root_session_id = session_id

        self._current_session_id = session_id
        return state

    def get(self, session_id: str) -> Optional[SessionState]:
        """Get session by ID."""
        return self.sessions.get(session_id)

    def get_root(self) -> Optional[SessionState]:
        """Get the root session."""
        if self.root_session_id:
            return self.sessions.get(self.root_session_id)
        return None

    def get_current(self) -> Optional[SessionState]:
        """Get the currently active session."""
        if self._current_session_id:
            return self.sessions.get(self._current_session_id)
        return self.get_root()

    def set_current(self, session_id: str) -> None:
        """Set the current active session."""
        if session_id in self.sessions:
            self._current_session_id = session_id

    def transition(self, session_id: str, phase: Phase) -> None:
        """Transition a session to a new phase."""
        if session_id in self.sessions:
            old_phase = self.sessions[session_id].phase
            self.sessions[session_id].phase = phase

            # Track thinking time
            state = self.sessions[session_id]
            if old_phase == Phase.THINKING and state.thinking_start:
                elapsed = (datetime.now() - state.thinking_start).total_seconds()
                state.metrics.thinking_time += elapsed
                state.thinking_start = None
            elif phase == Phase.THINKING:
                state.thinking_start = datetime.now()

    def get_breadcrumb(self, session_id: str) -> str:
        """Build an agent breadcrumb path like 'main → Explorer → Deep-Scan'."""
        parts: list[str] = []
        current = self.sessions.get(session_id)
        while current:
            name = current.agent_name or (
                "main" if current.depth == 0 else "sub-session"
            )
            parts.append(name)
            current = (
                self.sessions.get(current.parent_id) if current.parent_id else None
            )
        parts.reverse()
        return " → ".join(parts)
