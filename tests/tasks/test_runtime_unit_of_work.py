from __future__ import annotations

import sqlite3

import pytest

from app.agent.state import AgentPhase, AgentState
from app.project_identity import ProjectIdentity
from app.tasks.runtime_store import RuntimeStore
from app.tasks.session_store import SessionStore
from app.agent.session import SessionStatus
from app.tasks.models import (
    PlanStatus,
    StepStatus,
    TaskStatus,
    VerificationTargetType,
)
from app.tasks.verification_context import VerificationContext
from app.tasks.store_context import StoreContext
from app.tasks.unit_of_work import (
    ConcurrentWriterError,
    RuntimeUnitOfWork,
    StateAuthorityError,
)


def _context(tmp_path, name: str = "project") -> StoreContext:
    root = tmp_path / name
    root.mkdir()
    identity = ProjectIdentity.from_source_root(root)
    return StoreContext(
        database_path=tmp_path / "persistent_coder.db",
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
    )


def test_migrations_are_ordered_and_idempotent(tmp_path):
    context = _context(tmp_path)
    RuntimeStore(context)

    RuntimeUnitOfWork(context)
    RuntimeUnitOfWork(context)

    with sqlite3.connect(context.database_path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert versions == [(1,), (2,), (3,), (4,)]
    assert {
        "agent_state_snapshots",
        "budget_ledger",
        "file_operation_journal",
        "verifications",
    } <= tables


def test_verification_context_column_is_migrated_atomically(tmp_path):
    context = _context(tmp_path)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute(
            """
            CREATE TABLE verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                step_id INTEGER,
                status TEXT NOT NULL,
                reason TEXT,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    RuntimeUnitOfWork(context)

    with sqlite3.connect(context.database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(verifications)")
        }
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 4"
        ).fetchone()

    assert "context_json" in columns
    assert migration == ("revision-bound verification context",)


def test_state_transition_updates_cursor_and_snapshot_atomically(tmp_path):
    context = _context(tmp_path)
    runtime_store = RuntimeStore(context)
    authority = RuntimeUnitOfWork(context)
    run_id = runtime_store.start_run("build")
    state = AgentState(
        request="build",
        phase=AgentPhase.EXECUTING,
        plan_id=7,
        active_task_id=8,
        active_step_id=9,
        attempt_id=10,
        checkpoint_id="attempt-10",
    )

    snapshot_id = authority.persist_state(run_id, state)

    run = runtime_store.get_run(run_id)
    snapshot = authority.get_latest_snapshot(run_id)
    assert snapshot_id == snapshot["id"]
    assert run["phase"] == "EXECUTING"
    assert run["plan_id"] == 7
    assert run["task_id"] == 8
    assert run["step_id"] == 9
    assert run["attempt_id"] == 10
    assert run["checkpoint_id"] == "attempt-10"
    assert snapshot["phase"] == "EXECUTING"


def test_snapshot_insert_failure_rolls_back_cursor_update(tmp_path):
    context = _context(tmp_path)
    runtime_store = RuntimeStore(context)
    authority = RuntimeUnitOfWork(context)
    run_id = runtime_store.start_run("build")
    state = AgentState(request="build", phase=AgentPhase.EXECUTING)

    with sqlite3.connect(context.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_snapshot
            BEFORE INSERT ON agent_state_snapshots
            BEGIN
                SELECT RAISE(FAIL, 'snapshot rejected');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="snapshot rejected"):
        authority.persist_state(run_id, state)

    run = runtime_store.get_run(run_id)
    assert run["phase"] == "PLANNING"


def test_foreign_project_cannot_update_run(tmp_path):
    owner = _context(tmp_path, "owner")
    foreign = _context(tmp_path, "foreign")
    runtime_store = RuntimeStore(owner)
    run_id = runtime_store.start_run("build")
    authority = RuntimeUnitOfWork(foreign)

    with pytest.raises(StateAuthorityError, match="current project"):
        authority.persist_state(
            run_id,
            AgentState(request="build", phase=AgentPhase.EXECUTING),
        )

    assert runtime_store.get_run(run_id)["phase"] == "PLANNING"


def test_only_one_mandatory_writer_can_hold_the_database(tmp_path):
    context = _context(tmp_path)
    RuntimeStore(context)
    first = RuntimeUnitOfWork(context, timeout=0)
    second = RuntimeUnitOfWork(context, timeout=0)

    with first.transaction():
        with pytest.raises(ConcurrentWriterError):
            with second.transaction():
                pass


def test_terminal_outcome_commits_plan_run_snapshot_and_session_together(tmp_path):
    from helpers import make_stores, seed_plan

    project = tmp_path / "project"
    project.mkdir()
    stores = make_stores(tmp_path, project_root=project)
    plan_id, _, _ = seed_plan(stores)
    context = stores.plan_store.context
    runtime_store = RuntimeStore(context)
    session_store = SessionStore(context)
    session_id = session_store.create(sandbox_session_id="sandbox-1")
    session = session_store.get(session_id)
    run_id = runtime_store.start_run("build", sandbox_session_id="sandbox-1")
    session.transition(SessionStatus.RUNNING)
    session.active_run_id = run_id
    session = session_store.update(session)
    authority = RuntimeUnitOfWork(context)
    state = AgentState(
        request="build", phase=AgentPhase.DONE, plan_id=plan_id,
        completion="DONE",
    )

    new_version = authority.finish_terminal(
        run_id,
        state,
        run_status="DONE",
        plan_status=PlanStatus.DONE,
        session_id=session.id,
        session_status=SessionStatus.CLEAN,
        session_version=session.version,
    )

    assert runtime_store.get_run(run_id)["status"] == "DONE"
    assert stores.plan_store.get_plan(plan_id).status is PlanStatus.DONE
    settled = session_store.get(session.id)
    assert settled.status is SessionStatus.CLEAN
    assert settled.active_run_id is None
    assert settled.version == new_version
    assert authority.get_latest_snapshot(run_id)["phase"] == "DONE"


def test_terminal_database_failure_rolls_back_every_owner(tmp_path):
    from helpers import make_stores, seed_plan

    project = tmp_path / "project"
    project.mkdir()
    stores = make_stores(tmp_path, project_root=project)
    plan_id, _, _ = seed_plan(stores)
    context = stores.plan_store.context
    runtime_store = RuntimeStore(context)
    session_store = SessionStore(context)
    session_id = session_store.create(sandbox_session_id="sandbox-1")
    session = session_store.get(session_id)
    run_id = runtime_store.start_run("build", sandbox_session_id="sandbox-1")
    session.transition(SessionStatus.RUNNING)
    session.active_run_id = run_id
    session = session_store.update(session)
    authority = RuntimeUnitOfWork(context)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_terminal_session
            BEFORE UPDATE ON agent_sessions
            BEGIN SELECT RAISE(FAIL, 'session rejected'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="session rejected"):
        authority.finish_terminal(
            run_id,
            AgentState(
                request="build", phase=AgentPhase.DONE, plan_id=plan_id,
                completion="DONE",
            ),
            run_status="DONE",
            plan_status=PlanStatus.DONE,
            session_id=session.id,
            session_status=SessionStatus.CLEAN,
            session_version=session.version,
        )

    assert runtime_store.get_run(run_id)["status"] == "RUNNING"
    assert stores.plan_store.get_plan(plan_id).status is PlanStatus.ACTIVE
    assert session_store.get(session.id).status is SessionStatus.RUNNING
    assert authority.get_latest_snapshot(run_id) is None


def test_run_and_session_start_roll_back_together_on_session_conflict(tmp_path):
    context = _context(tmp_path)
    RuntimeStore(context)
    session_store = SessionStore(context)
    session_id = session_store.create(sandbox_session_id="sandbox-1")
    authority = RuntimeUnitOfWork(context)

    with pytest.raises(StateAuthorityError, match="session"):
        authority.start_run(
            "build",
            sandbox_session_id="sandbox-1",
            session_id=session_id,
            session_version=99,
        )

    assert RuntimeStore(context).get_running_runs() == []
    assert session_store.get(session_id).status is SessionStatus.CLEAN


def test_verification_record_and_step_transition_roll_back_together(tmp_path):
    from helpers import make_stores, seed_plan

    project = tmp_path / "project"
    project.mkdir()
    stores = make_stores(tmp_path, project_root=project)
    _plan_id, task, step = seed_plan(stores)
    stores.plan_store.update_task_status(task.id, TaskStatus.IN_PROGRESS)
    stores.step_store.update_step_status(step.id, StepStatus.IN_PROGRESS)
    stores.step_store.update_step_status(step.id, StepStatus.VERIFYING)
    authority = RuntimeUnitOfWork(stores.plan_store.context)
    with sqlite3.connect(stores.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_verified_step
            BEFORE UPDATE ON steps
            BEGIN SELECT RAISE(FAIL, 'step transition rejected'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="step transition rejected"):
        authority.finish_verification(
            target_type=VerificationTargetType.STEP,
            target_id=step.id,
            verification_status="PASS",
            evidence=["verified"],
            reason=None,
            context=VerificationContext("workspace", "spec", "environment"),
            target_status=StepStatus.DONE.value,
        )

    assert stores.step_store.get_step(step.id).status is StepStatus.VERIFYING
    assert stores.verification_store.get_step_verifications(step.id) == []
