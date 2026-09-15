from __future__ import annotations

import pytest

from app.agent.durable_attempts import AttemptRecoveryError, DurableAttemptCoordinator
from app.agent.state import AgentPhase, AgentState
from app.project_identity import ProjectIdentity
from app.tasks.models import AttemptStatus
from app.tasks.models import AttemptTargetType
from app.tasks.runtime_store import RuntimeStore
from app.tasks.store_context import StoreContext
from app.tasks.unit_of_work import RuntimeUnitOfWork

from helpers import make_stores, seed_plan


class _Workspace:
    session_id = "sandbox-1"

    def __init__(self):
        self.checkpoints: set[str] = set()
        self.rollbacks = 0

    def checkpoint(self, label):
        self.checkpoints.add(label)

    def has_checkpoint(self, label):
        return label in self.checkpoints

    def rollback(self, label):
        self.rollbacks += 1
        return label in self.checkpoints

    def commit(self, label):
        self.checkpoints.discard(label)


def _case(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    stores = make_stores(tmp_path, project_root=project)
    plan_id, task, step = seed_plan(stores)
    context = stores.plan_store.context
    runtime_store = RuntimeStore(context)
    authority = RuntimeUnitOfWork(context)
    run_id = runtime_store.start_run("build", sandbox_session_id="sandbox-1")
    state = AgentState(
        request="build", phase=AgentPhase.EXECUTING, plan_id=plan_id,
        active_task_id=task.id, active_step_id=step.id,
    )
    workspace = _Workspace()
    coordinator = DurableAttemptCoordinator(
        authority=authority, workspace=workspace, run_id=lambda: run_id
    )
    return stores, authority, coordinator, workspace, run_id, state, task, step


def test_crash_after_writable_action_rolls_back_once_and_is_idempotent(tmp_path):
    stores, authority, coordinator, workspace, run_id, state, task, step = _case(
        tmp_path
    )
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id

    coordinator.recover(authority.list_recovery_journals(run_id)[0])

    assert workspace.rollbacks == 1


def test_crash_before_checkpoint_marks_attempt_blocked_without_rollback(tmp_path):
    stores, authority, coordinator, workspace, run_id, state, _, step = _case(
        tmp_path
    )
    attempt_id = authority.prepare_attempt(
        run_id,
        state,
        target_type=AttemptTargetType.STEP,
        target_id=step.id,
        approach=None,
        sandbox_session_id=workspace.session_id,
        checkpoint_id="attempt-before-checkpoint",
    )

    coordinator.recover(authority.list_recovery_journals(run_id)[0])

    assert workspace.rollbacks == 0
    assert stores.attempt_store.get_attempt(attempt_id).status is AttemptStatus.BLOCKED
    assert authority.get_journal_for_attempt(attempt_id)["state"] == "ROLLED_BACK"


def test_crash_after_verification_still_rolls_back_uncommitted_files(tmp_path):
    stores, authority, coordinator, workspace, run_id, state, task, step = _case(
        tmp_path
    )
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id
    state.phase = AgentPhase.VERIFYING
    authority.persist_state(run_id, state)

    coordinator.recover(authority.list_recovery_journals(run_id)[0])

    assert workspace.rollbacks == 1
    assert stores.attempt_store.get_attempt(attempt_id).status is AttemptStatus.BLOCKED
    snapshot = authority.get_latest_snapshot(run_id)
    assert snapshot["phase"] == "VERIFYING"
    assert snapshot["attempt_id"] is None
    assert stores.attempt_store.get_attempt(attempt_id).status is AttemptStatus.BLOCKED
    assert authority.get_journal_for_attempt(attempt_id)["state"] == "ROLLED_BACK"
    assert coordinator.recover_pending(run_id) == []
    assert workspace.rollbacks == 1


def test_crash_after_filesystem_commit_completes_durable_commit(tmp_path):
    stores, authority, coordinator, workspace, run_id, state, task, step = _case(
        tmp_path
    )
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id
    authority.begin_attempt_finalization(run_id, attempt_id, commit=True)
    workspace.commit(state.checkpoint_id)

    coordinator.recover(authority.list_recovery_journals(run_id)[0])

    assert workspace.rollbacks == 0
    assert stores.attempt_store.get_attempt(attempt_id).status is AttemptStatus.PASS
    assert authority.get_journal_for_attempt(attempt_id)["state"] == "COMMITTED"


def test_crash_after_filesystem_rollback_completes_durable_rollback(tmp_path):
    stores, authority, coordinator, workspace, run_id, state, task, step = _case(
        tmp_path
    )
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id
    authority.begin_attempt_finalization(run_id, attempt_id, commit=False)
    assert workspace.rollback(state.checkpoint_id) is True
    workspace.commit(state.checkpoint_id)

    coordinator.recover(authority.list_recovery_journals(run_id)[0])

    assert stores.attempt_store.get_attempt(attempt_id).status is AttemptStatus.BLOCKED
    assert authority.get_journal_for_attempt(attempt_id)["state"] == "ROLLED_BACK"


def test_missing_ready_checkpoint_is_recorded_and_reported(tmp_path):
    _, authority, coordinator, workspace, run_id, state, task, step = _case(tmp_path)
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id
    workspace.checkpoints.clear()

    with pytest.raises(AttemptRecoveryError, match="missing"):
        coordinator.recover(authority.list_recovery_journals(run_id)[0])

    journal = authority.get_journal_for_attempt(attempt_id)
    assert journal["state"] == "RECOVERY_FAILED"
    assert "missing" in journal["error"]


def test_foreign_project_cannot_see_recovery_journal(tmp_path):
    _, authority, coordinator, _, run_id, state, task, step = _case(tmp_path)
    coordinator.begin(state, task, step)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    identity = ProjectIdentity.from_source_root(foreign_root)
    foreign = RuntimeUnitOfWork(
        StoreContext(
            database_path=authority.database_path,
            project_id=identity.project_id,
            canonical_source_root=str(identity.canonical_source_root),
        )
    )

    assert foreign.list_recovery_journals(run_id) == []
