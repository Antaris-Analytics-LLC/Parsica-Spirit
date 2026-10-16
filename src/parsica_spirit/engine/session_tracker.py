"""
SessionTracker - trigger-based multi-session write isolation for Parsica Memory.

Tracks which sessions are active via explicit runtime signals (begin_turn /
end_turn), manages named pending tasks for sub-agent coordination, and provides
a write gate that prevents idle sessions from mutating the memory store.

Architecture: trigger-based state machine. Sessions transition between ACTIVE
and IDLE via explicit runtime calls - NOT via time-based TTL. A crash safety
net (cleanup_stale) exists only to catch orphaned sessions where end_turn()
was never called due to a bug or crash.

State machine:
    IDLE -> begin_turn() -> ACTIVE
    ACTIVE -> end_turn() (if turn_depth==0 AND pending_tasks==0) -> IDLE
    ACTIVE -> end_turn() (if tasks pending or nested turns) -> stays ACTIVE

Isolation is for writes, not reads: the gate scopes what a session may WRITE;
what a session can recall is governed separately (cross_session_recall).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("parsica_spirit.engine")


def sid_tag(session_id: object) -> str:
    """A short, non-reversible tag that stands in for a session id in log lines.

    In CLOSED mode a registered session id is the write capability, and a warning line travels
    further than the tracker does (a log file, a transcript, a chat mirror), so the id itself never
    reaches one whole. Two log lines about the same session still carry the same tag."""
    digest = hashlib.sha256(str(session_id).encode("utf-8", "replace")).hexdigest()[:10]
    return "sid:" + digest

# Safety net TTL - crash recovery ONLY. If this fires in normal operation,
# it means the runtime has a bug (end_turn was never called).
SAFETY_TTL_SECONDS: int = 1800  # 30 minutes

# Maximum pending tasks per session (prevents unbounded growth)
MAX_PENDING_TASKS: int = 50

# Maximum tracked sessions (LRU eviction beyond this)
MAX_TRACKED_SESSIONS: int = 500

# Minimum interval between safety sweeps (seconds)
_SWEEP_INTERVAL: int = 60


@dataclass
class SessionState:
    """Snapshot of a single session's state in the trigger-based model."""

    session_id: str
    status: str = "idle"  # "active" | "idle"
    turn_depth: int = 0  # Reference count for concurrent/nested turns
    pending_tasks: Dict[str, float] = field(default_factory=dict)  # task_name -> timestamp
    last_active: float = 0.0  # time.time() of last state change


class SessionTracker:
    """Trigger-based session state tracker for multi-session write isolation.

    Designed to be composed into MemorySystem::

        self._session_tracker = SessionTracker()

    State transitions are driven by explicit runtime signals:
    - begin_turn(session_id) - session becomes ACTIVE
    - end_turn(session_id) - session becomes IDLE (when turn_depth==0 and no tasks)
    - add_task(session_id, task_name) - registers a named pending task
    - complete_task(session_id, task_name) - completes a named task

    When no sessions have been registered (backward compat), all writes
    are allowed in OPEN mode. In CLOSED mode the gate is strict from the
    very first write.

    Thread safety: NOT thread-safe in v1 (single-process assumption).
    """

    def __init__(
        self,
        mode: str = "closed",
        max_sessions: int = MAX_TRACKED_SESSIONS,
        max_pending_tasks: int = MAX_PENDING_TASKS,
        safety_ttl_seconds: int = SAFETY_TTL_SECONDS,
    ) -> None:
        self._sessions: Dict[str, SessionState] = {}
        self._mode = mode
        self._max_sessions = max_sessions
        self._max_pending_tasks = max_pending_tasks
        self._safety_ttl = safety_ttl_seconds
        self._last_sweep: float = 0.0

    # -- session ID validation --------------------------------------------

    @staticmethod
    def _validate_session_id(session_id: object) -> str:
        """Validate and sanitize a session ID.

        Rejects None, empty string, and IDs containing control characters
        (including newlines, carriage returns, tabs, and other ASCII/Unicode
        control characters).  Returns the ID as-is if valid.

        Raises:
            TypeError:  if session_id is None or not a string.
            ValueError: if session_id is empty or contains control characters.
        """
        if session_id is None:
            raise TypeError("session_id must be a string, got None")
        if not isinstance(session_id, str):
            raise TypeError(
                f"session_id must be a string, got {type(session_id).__name__}"
            )
        if not session_id or not session_id.strip():
            raise ValueError("session_id must not be empty or whitespace-only")
        # Reject any control characters (U+0000-U+001F, U+007F, U+0080-U+009F)
        # including \n, \r, \t which are the primary prompt-injection vectors.
        for ch in session_id:
            cp = ord(ch)
            if cp <= 0x1F or cp == 0x7F or (0x80 <= cp <= 0x9F):
                raise ValueError(
                    f"session_id contains illegal control character "
                    f"(U+{cp:04X}): {repr(session_id)}"
                )
        return session_id

    # -- lifecycle signals (called by runtime) ----------------------------

    def begin_turn(self, session_id: str) -> None:
        """Signal that a session has received an inbound message.

        Increments turn_depth (reference counting for concurrent turns),
        sets status to ACTIVE, and auto-registers the session if new.

        Also runs a lazy safety sweep to catch orphaned sessions.

        Raises:
            TypeError:  if session_id is None or not a string.
            ValueError: if session_id is empty or contains control characters.
        """
        session_id = self._validate_session_id(session_id)
        self._maybe_safety_sweep()
        state = self._ensure_session(session_id)
        state.status = "active"
        state.turn_depth += 1
        state.last_active = time.time()

    def end_turn(self, session_id: str) -> None:
        """Signal that the runtime has finished processing the current turn.

        Decrements turn_depth. The session transitions to IDLE only when:
        - turn_depth reaches 0 (all nested/concurrent turns complete)
        - pending_tasks is empty (all sub-agents have reported back)

        If either condition is not met, the session stays ACTIVE.
        Calling end_turn on an unknown session is a no-op (defensive).
        Calling end_turn when turn_depth is already 0 is a no-op with warning.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return  # Unknown session - no-op

        if state.turn_depth <= 0:
            logger.warning(
                "end_turn(%s): turn_depth already 0 - ignoring "
                "(possible mismatched begin_turn/end_turn)",
                sid_tag(session_id),
            )
            return

        state.turn_depth -= 1
        state.last_active = time.time()

        if state.turn_depth == 0 and not state.pending_tasks:
            state.status = "idle"

    # -- task tracking ----------------------------------------------------

    def add_task(self, session_id: str, task_name: str) -> bool:
        """Register a named pending task (e.g. sub-agent spawned).

        Keeps the session ACTIVE as long as any tasks are pending.
        Returns True if the task was added, False if rejected (session
        not registered, or at the per-session task limit).

        Does NOT auto-register sessions - the session must already exist
        (created via begin_turn).
        """
        state = self._sessions.get(session_id)
        if state is None:
            logger.warning(
                "add_task(%s, %s): session not registered - ignoring. "
                "Call begin_turn() first.",
                sid_tag(session_id),
                task_name,
            )
            return False

        if len(state.pending_tasks) >= self._max_pending_tasks:
            logger.warning(
                "add_task(%s, %s): at limit (%d) - rejecting",
                sid_tag(session_id),
                task_name,
                self._max_pending_tasks,
            )
            return False

        state.pending_tasks[task_name] = time.time()
        state.last_active = time.time()
        return True

    def complete_task(self, session_id: str, task_name: str) -> None:
        """Mark a named pending task as complete.

        Removes the task from the registry. If this was the last pending
        task AND turn_depth == 0, the session transitions to IDLE.

        Completing a task that doesn't exist logs a warning but does not
        raise. Defensive - task completion should never crash.
        """
        state = self._sessions.get(session_id)
        if state is None:
            logger.warning(
                "complete_task(%s, %s): session not registered - ignoring",
                sid_tag(session_id),
                task_name,
            )
            return

        if task_name not in state.pending_tasks:
            logger.warning(
                "complete_task(%s, %s): task not found in pending_tasks - ignoring",
                sid_tag(session_id),
                task_name,
            )
            return

        del state.pending_tasks[task_name]
        state.last_active = time.time()

        # Check if we should transition to idle
        if state.turn_depth == 0 and not state.pending_tasks:
            state.status = "idle"

    # -- write gate -------------------------------------------------------

    def is_write_allowed(self, session_id: Optional[str]) -> bool:
        """Determine if a session can write to the memory store.

        Behavior depends on mode (open vs closed):

        CLOSED mode (strict):
            - No sessions tracked -> DENY (strict from the very first write)
            - session_id is None -> DENY (when sessions are tracked)
            - Session not registered -> DENY
            - Session ACTIVE -> allow
            - Session IDLE -> DENY

        OPEN mode (permissive):
            - Everything is allowed, but idle/unregistered writes are logged.
              The engine's own dedup remains the guard against pollution.
        """
        self._maybe_safety_sweep()

        # Backward compat: no sessions tracked = allowed in OPEN mode only.
        # In CLOSED mode, the gate is strict from the very first write -
        # callers must call begin_turn() before any write, even on cold start.
        if not self._sessions:
            if self._mode == "open":
                return True
            # CLOSED mode: no sessions tracked = deny (strict from first write)
            if session_id is not None:
                logger.warning(
                    "CLOSED mode: write denied on cold start for session %s. "
                    "Call begin_turn() with that id first.",
                    sid_tag(session_id),
                )
            return self._mode != "closed"

        if self._mode == "open":
            return self._is_write_allowed_open(session_id)
        else:
            return self._is_write_allowed_closed(session_id)

    def _is_write_allowed_open(self, session_id: Optional[str]) -> bool:
        """OPEN mode: permissive gate. Warns but allows."""
        if session_id is None:
            return True

        state = self._sessions.get(session_id)
        if state is None:
            logger.warning(
                "OPEN mode: unregistered session %s writing (allowed but logged)",
                sid_tag(session_id),
            )
            return True

        if state.status != "active":
            logger.warning(
                "OPEN mode: idle session %s writing (allowed but logged)",
                sid_tag(session_id),
            )

        return True

    def _is_write_allowed_closed(self, session_id: Optional[str]) -> bool:
        """CLOSED mode: strict gate. Only ACTIVE sessions can write."""
        if session_id is None:
            logger.warning(
                "CLOSED mode: anonymous write denied (sessions are tracked)"
            )
            return False

        state = self._sessions.get(session_id)

        if state is None:
            # CLOSED mode: unregistered sessions are BLOCKED.
            # Only begin_turn() can register a session. This is the key
            # difference from OPEN mode - no fail-open bypass.
            logger.warning(
                "CLOSED mode: write denied for unregistered session %s. "
                "Call begin_turn() with that id first.",
                sid_tag(session_id),
            )
            return False

        if state.status == "active":
            return True

        logger.debug(
            "Write denied: session %s is idle (last_active=%.1f)",
            sid_tag(session_id),
            state.last_active,
        )
        return False

    # -- escape hatch -----------------------------------------------------

    def force_idle(self, session_id: str) -> None:
        """Force a session to IDLE regardless of turn_depth or pending tasks.

        Escape hatch for when the runtime knows a session is done but
        turn counts or tasks have leaked (e.g. sub-agent crashed without
        reporting completion).
        """
        state = self._sessions.get(session_id)
        if state is None:
            return

        if state.turn_depth > 0 or state.pending_tasks:
            logger.info(
                "force_idle(%s): clearing turn_depth=%d, %d pending tasks",
                sid_tag(session_id),
                state.turn_depth,
                len(state.pending_tasks),
            )

        state.status = "idle"
        state.turn_depth = 0
        state.pending_tasks.clear()
        state.last_active = time.time()

    # -- queries ----------------------------------------------------------

    def is_active(self, session_id: str) -> bool:
        """Check if a session is currently active."""
        state = self._sessions.get(session_id)
        if state is None:
            return False
        return state.status == "active"

    def get_active_sessions(self) -> List[str]:
        """Return list of currently active session IDs."""
        return [
            sid
            for sid, state in self._sessions.items()
            if state.status == "active"
        ]

    def get_states(self) -> Dict[str, SessionState]:
        """Return all session states for debugging/observability."""
        return dict(self._sessions)

    def get_session_state(self, session_id: str) -> Optional[SessionState]:
        """Return the state of a specific session, or None if not tracked."""
        return self._sessions.get(session_id)

    def has_any_sessions(self) -> bool:
        """Return True if at least one session has been registered."""
        return bool(self._sessions)

    # -- crash safety net -------------------------------------------------

    def cleanup_stale(self, max_idle_seconds: int = 3600) -> int:
        """Remove sessions that have been idle longer than max_idle_seconds.

        This is a SAFETY NET for crash recovery - not the primary idle
        mechanism. In normal operation, sessions go idle via end_turn().

        Also sweeps for orphaned ACTIVE sessions (stuck due to missing
        end_turn calls) using the safety TTL.

        Returns:
            Number of sessions removed.
        """
        now = time.time()

        # First: force-idle any sessions stuck ACTIVE beyond safety TTL
        self._safety_sweep_now(now)

        # Then: remove stale idle sessions
        stale_ids = []
        for sid, state in self._sessions.items():
            if state.status == "idle" and (now - state.last_active) > max_idle_seconds:
                stale_ids.append(sid)

        for sid in stale_ids:
            logger.info(
                "cleanup_stale: removing session %s (idle for %.0fs)",
                sid_tag(sid),
                now - self._sessions[sid].last_active,
            )
            del self._sessions[sid]

        return len(stale_ids)

    # -- internal helpers -------------------------------------------------

    def _ensure_session(self, session_id: str) -> SessionState:
        """Get or create a session state. New sessions start as IDLE."""
        state = self._sessions.get(session_id)
        if state is None:
            self._enforce_session_limit()
            state = SessionState(
                session_id=session_id,
                last_active=time.time(),
            )
            self._sessions[session_id] = state
        return state

    def _enforce_session_limit(self) -> None:
        """Evict oldest sessions if at the session limit.

        Evicts idle sessions first (LRU), then oldest active sessions
        if still over limit. This prevents DoS via active-session flooding.
        """
        if len(self._sessions) < self._max_sessions:
            return

        evict_count = len(self._sessions) - self._max_sessions + 1

        # Phase 1: evict idle sessions first (oldest first)
        idle_sessions = sorted(
            (
                (state.last_active, sid)
                for sid, state in self._sessions.items()
                if state.status == "idle"
            ),
        )
        for _, sid in idle_sessions[:evict_count]:
            logger.info(
                "Session limit reached (%d): evicting idle session %s",
                self._max_sessions,
                sid_tag(sid),
            )
            del self._sessions[sid]
            evict_count -= 1

        if evict_count <= 0:
            return

        # Phase 2: if still over limit, evict oldest active sessions
        active_sessions = sorted(
            (
                (state.last_active, sid)
                for sid, state in self._sessions.items()
                if state.status == "active"
            ),
        )
        for _, sid in active_sessions[:evict_count]:
            logger.warning(
                "Session limit HARD CAP (%d): evicting ACTIVE session %s (DoS prevention)",
                self._max_sessions,
                sid_tag(sid),
            )
            del self._sessions[sid]

    def _maybe_safety_sweep(self) -> None:
        """Run safety sweep at most once per _SWEEP_INTERVAL seconds."""
        now = time.time()
        if now - self._last_sweep < _SWEEP_INTERVAL:
            return
        self._last_sweep = now
        self._safety_sweep_now(now)

    def _safety_sweep_now(self, now: Optional[float] = None) -> None:
        """Sweep for orphaned ACTIVE sessions beyond the safety TTL.

        This is NOT the primary lifecycle mechanism. It's a safety net
        for bugs where end_turn() is never called. In normal operation,
        this should never trigger. If it does, it means there's a bug
        in the runtime - end_turn() was never called.

        Logged at ERROR level because it indicates a runtime bug.
        """
        if now is None:
            now = time.time()

        for sid, state in list(self._sessions.items()):
            if state.status == "active":
                elapsed = now - state.last_active
                if elapsed > self._safety_ttl:
                    logger.error(
                        "SAFETY NET: Session %s has been ACTIVE for %.0fs "
                        "without any activity. Forcing to IDLE. This indicates "
                        "a bug in the runtime - end_turn() was never called.",
                        sid_tag(sid),
                        elapsed,
                    )
                    state.status = "idle"
                    state.turn_depth = 0
                    state.pending_tasks.clear()
                    state.last_active = now
