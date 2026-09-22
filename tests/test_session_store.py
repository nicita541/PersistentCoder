from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.session import SessionStatus
from app.tasks.session_store import (
    SessionConflictError,
    SessionStore,
)
from app.tasks.store_context import StoreContext


def _context(
    database: Path,
    marker: str,
    root: Path,
) -> StoreContext:
    root.mkdir(exist_ok=True)
    return StoreContext(
        database_path=database,
        project_id=marker * 64,
        canonical_source_root=str(root.resolve()),
    )


def test_session_store_is_project_scoped(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    store_a = SessionStore(context_a)
    store_b = SessionStore(context_b)

    session_id = store_a.create(sandbox_session_id="sandbox-a")

    session = store_a.get(session_id)
    assert session is not None
    assert session.project_id == "a" * 64
    assert session.status is SessionStatus.CLEAN
    assert store_b.get(session_id) is None


def test_latest_record_for_reused_sandbox_session_wins(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path / "shared.db",
        "a",
        tmp_path / "project",
    )
    store = SessionStore(context)
    first_id = store.create(sandbox_session_id="sandbox-a")
    second_id = store.create(sandbox_session_id="sandbox-a")

    latest = store.get_by_sandbox_session_id("sandbox-a")

    assert latest is not None
    assert latest.id == second_id
    assert latest.id != first_id


def test_stale_session_update_fails_closed(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path / "sessions.db",
        "a",
        tmp_path / "a",
    )
    store = SessionStore(context)
    session_id = store.create(sandbox_session_id="sandbox-a")
    first = store.get(session_id)
    stale = store.get(session_id)
    assert first is not None and stale is not None

    first.transition(SessionStatus.RUNNING)
    updated = store.update(first)
    assert updated.version == 1

    stale.transition(SessionStatus.RUNNING)
    with pytest.raises(SessionConflictError):
        store.update(stale)


def test_list_dirty_returns_only_current_project(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    store_a = SessionStore(context_a)
    store_b = SessionStore(context_b)
    session_id = store_a.create(sandbox_session_id="sandbox-a")
    session = store_a.get(session_id)
    assert session is not None
    session.transition(SessionStatus.RUNNING)
    session.transition(SessionStatus.DIRTY_FAILED)
    store_a.update(session)

    assert [item.id for item in store_a.list_dirty()] == [session_id]
    assert store_b.list_dirty() == []


def test_list_unsettled_includes_committed_session_pending_rebase(
    tmp_path: Path,
) -> None:
    context = _context(
        tmp_path / "sessions.db",
        "a",
        tmp_path / "project",
    )
    store = SessionStore(context)
    session_id = store.create(sandbox_session_id="sandbox-a")
    session = store.get(session_id)
    assert session is not None
    session.transition(SessionStatus.RUNNING)
    session.transition(SessionStatus.DIRTY_VERIFIED)
    session.transition(SessionStatus.APPLIED)
    store.update(session)

    assert store.list_dirty() == []
    assert [item.id for item in store.list_unsettled()] == [session_id]
