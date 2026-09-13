from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SessionStatus(str, Enum):
    CLEAN = "CLEAN"
    RUNNING = "RUNNING"
    DIRTY_VERIFIED = "DIRTY_VERIFIED"
    DIRTY_FAILED = "DIRTY_FAILED"
    APPLIED = "APPLIED"
    DISCARDED = "DISCARDED"


class SessionTransitionError(RuntimeError):
    pass


ALLOWED_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.CLEAN: frozenset({SessionStatus.RUNNING}),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.DIRTY_VERIFIED,
            SessionStatus.DIRTY_FAILED,
            SessionStatus.CLEAN,
        }
    ),
    SessionStatus.DIRTY_VERIFIED: frozenset(
        {SessionStatus.APPLIED, SessionStatus.DISCARDED}
    ),
    SessionStatus.DIRTY_FAILED: frozenset({SessionStatus.DISCARDED}),
    SessionStatus.APPLIED: frozenset({SessionStatus.CLEAN}),
    SessionStatus.DISCARDED: frozenset({SessionStatus.CLEAN}),
}


@dataclass(slots=True)
class AgentSession:
    id: str
    project_id: str
    canonical_source_root: str
    sandbox_session_id: str
    status: SessionStatus
    active_run_id: int | None
    patch_manifest_id: str | None
    version: int
    created_at: str | None = None
    updated_at: str | None = None

    def transition(self, target: SessionStatus) -> None:
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise SessionTransitionError(
                f"illegal session transition: {self.status.value} "
                f"-> {target.value}"
            )
        self.status = target
