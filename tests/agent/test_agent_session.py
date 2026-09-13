from __future__ import annotations

import pytest

from app.agent.session import (
    AgentSession,
    SessionStatus,
    SessionTransitionError,
)


def _session(status: SessionStatus) -> AgentSession:
    return AgentSession(
        id="session-record",
        project_id="a" * 64,
        canonical_source_root="C:/project-a",
        sandbox_session_id="sandbox-a",
        status=status,
        active_run_id=None,
        patch_manifest_id=None,
        version=0,
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SessionStatus.CLEAN, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.DIRTY_VERIFIED),
        (SessionStatus.RUNNING, SessionStatus.DIRTY_FAILED),
        (SessionStatus.RUNNING, SessionStatus.CLEAN),
        (SessionStatus.DIRTY_VERIFIED, SessionStatus.APPLIED),
        (SessionStatus.DIRTY_VERIFIED, SessionStatus.DISCARDED),
        (SessionStatus.DIRTY_FAILED, SessionStatus.DISCARDED),
        (SessionStatus.APPLIED, SessionStatus.CLEAN),
        (SessionStatus.DISCARDED, SessionStatus.CLEAN),
    ],
)
def test_allowed_session_transitions(
    source: SessionStatus,
    target: SessionStatus,
) -> None:
    session = _session(source)

    session.transition(target)

    assert session.status is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SessionStatus.CLEAN, SessionStatus.APPLIED),
        (SessionStatus.CLEAN, SessionStatus.DIRTY_VERIFIED),
        (SessionStatus.DIRTY_FAILED, SessionStatus.APPLIED),
        (SessionStatus.APPLIED, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.APPLIED),
    ],
)
def test_illegal_session_transitions_fail_closed(
    source: SessionStatus,
    target: SessionStatus,
) -> None:
    session = _session(source)

    with pytest.raises(SessionTransitionError):
        session.transition(target)

    assert session.status is source
