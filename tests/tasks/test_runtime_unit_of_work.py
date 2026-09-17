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

    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,)]
    assert {
        "agent_state_snapshots",
        "budget_ledger",
        "file_operation_journal",
        "verifications",
        "patch_manifests",
        "patch_manifest_entries",
        "apply_journal",
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


def _manifest_terminal_fixture(tmp_path):
    from app.apply.manifest import PatchEntry, PatchManifest, PatchOperation

    context = _context(tmp_path)
    RuntimeStore(context)
    sessions = SessionStore(context)
    session_id = sessions.create(sandbox_session_id="sandbox-1")
    authority = RuntimeUnitOfWork(context)
    run_id, version = authority.start_run("build", sandbox_session_id="sandbox-1", session_id=session_id, session_version=0)
    manifest = PatchManifest.create(
        project_id=context.project_id, canonical_source_root=context.canonical_source_root,
        session_id="sandbox-1", baseline_sha256="a" * 64, workspace_sha256="b" * 64,
        verification_id="verification-1",
        entries=[PatchEntry("a.py", PatchOperation.ADD, None, "c" * 64, None, 1, True, True, ())],
    )
    return context, sessions, authority, run_id, session_id, version, manifest


def _finish_with_manifest(authority, run_id, session_id, version, manifest, **overrides):
    arguments = dict(run_status="DONE", plan_status=None, session_id=session_id,
                     session_status=SessionStatus.DIRTY_VERIFIED, session_version=version,
                     patch_manifest=manifest)
    arguments.update(overrides)
    return authority.finish_terminal(run_id, AgentState(request="build", phase=AgentPhase.DONE, completion="DONE"), **arguments)


def test_success_terminal_inserts_manifest_and_binds_session_atomically(tmp_path):
    context, sessions, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    new_version = _finish_with_manifest(authority, run_id, session_id, version, manifest)
    from app.apply.store import PatchManifestStore
    assert PatchManifestStore(context).get(manifest.manifest_id) == manifest
    settled = sessions.get(session_id)
    assert settled.patch_manifest_id == manifest.manifest_id
    assert settled.status is SessionStatus.DIRTY_VERIFIED
    assert settled.version == new_version == version + 1
    assert RuntimeStore(context).get_run(run_id)["status"] == "DONE"


@pytest.mark.parametrize("failure", ["session", "entry", "snapshot", "version"])
def test_manifest_terminal_failure_leaves_no_partial_state(tmp_path, failure):
    context, sessions, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    if failure != "version":
        table = {"session": "agent_sessions", "entry": "patch_manifest_entries", "snapshot": "agent_state_snapshots"}[failure]
        action = "UPDATE" if failure == "session" else "INSERT"
        with sqlite3.connect(context.database_path) as connection:
            connection.execute(f"CREATE TRIGGER fail_terminal BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises((sqlite3.IntegrityError, StateAuthorityError)):
        _finish_with_manifest(authority, run_id, session_id, version + (failure == "version"), manifest)
    from app.apply.store import PatchManifestStore
    assert PatchManifestStore(context).get(manifest.manifest_id) is None
    assert sessions.get(session_id).status is SessionStatus.RUNNING
    assert sessions.get(session_id).patch_manifest_id is None
    assert RuntimeStore(context).get_run(run_id)["status"] == "RUNNING"
    assert authority.get_latest_snapshot(run_id) is None


@pytest.mark.parametrize("run_status", ["FAILED", "INTERRUPTED"])
def test_failed_terminal_cannot_bind_manifest_or_clear_prior_binding(tmp_path, run_status):
    context, sessions, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    with pytest.raises((ValueError, StateAuthorityError)):
        _finish_with_manifest(authority, run_id, session_id, version, manifest, run_status=run_status, session_status=SessionStatus.DIRTY_FAILED)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("UPDATE agent_sessions SET patch_manifest_id = 'prior-manifest' WHERE id = ?", (session_id,))
    authority.finish_terminal(run_id, AgentState(request="build", phase=AgentPhase.FAILED), run_status=run_status, plan_status=None, session_id=session_id, session_status=SessionStatus.DIRTY_FAILED, session_version=version)
    assert sessions.get(session_id).patch_manifest_id == "prior-manifest"


def test_terminal_manifest_requires_matching_owner_sandbox_and_active_run(tmp_path):
    context, sessions, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("UPDATE agent_sessions SET sandbox_session_id = 'other' WHERE id = ?", (session_id,))
    with pytest.raises((ValueError, StateAuthorityError), match="session|sandbox"):
        _finish_with_manifest(authority, run_id, session_id, version, manifest)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("UPDATE agent_sessions SET sandbox_session_id = 'sandbox-1', active_run_id = 999 WHERE id = ?", (session_id,))
    with pytest.raises(StateAuthorityError, match="session|run"):
        _finish_with_manifest(authority, run_id, session_id, version, manifest)


def test_terminal_refuses_manifest_saved_in_a_separate_transaction(tmp_path):
    context, sessions, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    from app.apply.store import PatchManifestStore
    PatchManifestStore(context).save(manifest, agent_session_id=session_id)
    with pytest.raises((ValueError, StateAuthorityError), match="transaction|already"):
        _finish_with_manifest(authority, run_id, session_id, version, manifest)
    assert sessions.get(session_id).status is SessionStatus.RUNNING


def _prepared_apply(tmp_path):
    context, sessions, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    version = _finish_with_manifest(authority, run_id, session_id, version, manifest)
    journal_id, version = authority.prepare_apply(session_id=session_id, manifest_id=manifest.manifest_id, session_version=version, staging_id="stage-1", backup_id="backup-1")
    return context, sessions, authority, session_id, journal_id, version


def test_apply_journal_cas_commits_session_and_journal_together(tmp_path):
    _, sessions, authority, session_id, journal_id, version = _prepared_apply(tmp_path)
    assert authority.get_apply_journal(journal_id)["state"] == "PREPARING"
    version = authority.transition_apply(journal_id, expected_state="PREPARING", target_state="APPLYING", session_version=version)
    version = authority.transition_apply(journal_id, expected_state="APPLYING", target_state="COMMITTED", session_version=version)
    assert authority.get_apply_journal(journal_id)["state"] == "COMMITTED"
    assert sessions.get(session_id).status is SessionStatus.APPLIED
    assert sessions.get(session_id).version == version
    assert authority.pending_apply_journals() == []


@pytest.mark.parametrize("target", ["CONFLICT", "ROLLED_BACK", "RECOVERY_FAILED"])
def test_apply_failure_keeps_manifest_and_records_bounded_error(tmp_path, target):
    _, sessions, authority, session_id, journal_id, version = _prepared_apply(tmp_path)
    before = sessions.get(session_id).patch_manifest_id
    authority.transition_apply(journal_id, expected_state="PREPARING", target_state=target, session_version=version, error="x" * 5000)
    journal = authority.get_apply_journal(journal_id)
    assert journal["state"] == target
    assert len(journal["error"]) <= 2000
    assert sessions.get(session_id).patch_manifest_id == before
    assert sessions.get(session_id).status is SessionStatus.DIRTY_VERIFIED
    assert bool(authority.pending_apply_journals()) == (target == "RECOVERY_FAILED")


def test_apply_rejects_stale_illegal_and_foreign_transitions(tmp_path):
    _, sessions, authority, session_id, journal_id, version = _prepared_apply(tmp_path)
    for source, target, supplied_version in [("PREPARING", "COMMITTED", version), ("APPLYING", "COMMITTED", version), ("PREPARING", "APPLYING", version - 1)]:
        with pytest.raises((ValueError, StateAuthorityError)):
            authority.transition_apply(journal_id, expected_state=source, target_state=target, session_version=supplied_version)
    foreign = RuntimeUnitOfWork(_context(tmp_path, "foreign"))
    assert foreign.get_apply_journal(journal_id) is None
    assert foreign.pending_apply_journals() == []
    with pytest.raises(StateAuthorityError):
        foreign.transition_apply(journal_id, expected_state="PREPARING", target_state="APPLYING", session_version=version)
    assert authority.get_apply_journal(journal_id)["state"] == "PREPARING"
    assert sessions.get(session_id).version == version


def test_apply_session_failure_rolls_back_journal_transition(tmp_path):
    context, _, authority, _, journal_id, version = _prepared_apply(tmp_path)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("CREATE TRIGGER fail_apply BEFORE UPDATE ON agent_sessions BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        authority.transition_apply(journal_id, expected_state="PREPARING", target_state="APPLYING", session_version=version)
    assert authority.get_apply_journal(journal_id)["state"] == "PREPARING"


@pytest.mark.parametrize("identifier", ["../outside", "/tmp/stage", "C:\\stage", "a/b", "a\\b", ".", "", "x" * 129])
def test_apply_rejects_nonlocal_artifact_identifiers(tmp_path, identifier):
    _, _, authority, run_id, session_id, version, manifest = _manifest_terminal_fixture(tmp_path)
    version = _finish_with_manifest(authority, run_id, session_id, version, manifest)
    with pytest.raises(ValueError, match="identifier"):
        authority.prepare_apply(session_id=session_id, manifest_id=manifest.manifest_id, session_version=version, staging_id=identifier, backup_id="backup-1")


def test_apply_preparation_prevents_overlapping_writers(tmp_path):
    _, sessions, authority, session_id, journal_id, version = _prepared_apply(tmp_path)
    with pytest.raises((StateAuthorityError, sqlite3.IntegrityError)):
        authority.prepare_apply(session_id=session_id, manifest_id=sessions.get(session_id).patch_manifest_id, session_version=version, staging_id="stage-2", backup_id="backup-2")
    assert len(authority.pending_apply_journals()) == 1
