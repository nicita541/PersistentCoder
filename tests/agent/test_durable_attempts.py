from __future__ import annotations

import pytest

from app.agent.controller import AgentController, AgentControllerError
from app.agent.durable_attempts import (
    AttemptPreparationError,
    DurableAttemptCoordinator,
)
from app.agent.state import AgentPhase, AgentState
from app.tasks.runtime_store import RuntimeStore
from app.tasks.unit_of_work import RuntimeUnitOfWork

from helpers import make_stores, seed_plan


class _Workspace:
    session_id = "sandbox-1"

    def __init__(
        self,
        *,
        checkpoint_error: Exception | None = None,
        rollback_error: Exception | None = None,
    ):
        self.checkpoint_error = checkpoint_error
        self.rollback_error = rollback_error
        self.checkpoints: set[str] = set()

    def checkpoint(self, label: str):
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        self.checkpoints.add(label)

    def has_checkpoint(self, label: str) -> bool:
        return label in self.checkpoints

    def rollback(self, label: str) -> bool:
        if self.rollback_error is not None:
            raise self.rollback_error
        return label in self.checkpoints

    def commit(self, label: str) -> None:
        self.checkpoints.discard(label)


class _Coder:
    def __init__(self):
        self.calls = 0

    def execute(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("coder must not run")


def _coordinator(tmp_path, workspace):
    project = tmp_path / "project"
    project.mkdir()
    stores = make_stores(tmp_path, project_root=project)
    plan_id, task, step = seed_plan(stores)
    context = stores.plan_store.context
    runtime_store = RuntimeStore(context)
    authority = RuntimeUnitOfWork(context)
    run_id = runtime_store.start_run(
        "build", sandbox_session_id=workspace.session_id
    )
    coordinator = DurableAttemptCoordinator(
        authority=authority,
        workspace=workspace,
        run_id=lambda: run_id,
    )
    return stores, runtime_store, authority, coordinator, run_id, plan_id, task, step


def test_checkpoint_failure_blocks_coder_and_leaves_durable_attempt(tmp_path):
    workspace = _Workspace(checkpoint_error=OSError("disk full"))
    stores, _, authority, coordinator, run_id, plan_id, task, step = (
        _coordinator(tmp_path, workspace)
    )
    coder = _Coder()
    controller = AgentController(
        planner=object(),
        coder=coder,
        verifier=object(),
        repair=object(),
        scheduler=object(),
        plan_store=stores.plan_store,
        step_store=stores.step_store,
        attempt_store=stores.attempt_store,
        attempt_coordinator=coordinator,
    )
    state = AgentState(
        request="build",
        phase=AgentPhase.EXECUTING,
        plan_id=plan_id,
        active_task_id=task.id,
        active_step_id=step.id,
    )

    with pytest.raises(AttemptPreparationError, match="disk full"):
        controller.execute(state)

    assert coder.calls == 0
    attempts = stores.attempt_store.get_step_attempts(step.id)
    assert len(attempts) == 1
    journal = authority.get_journal_for_attempt(attempts[0].id)
    assert journal["state"] == "PREPARING"
    assert "disk full" in journal["error"]
    assert authority.get_latest_snapshot(run_id)["attempt_id"] == attempts[0].id


def test_database_failure_before_attempt_blocks_checkpoint_and_coder(tmp_path):
    workspace = _Workspace()
    stores, _, authority, coordinator, _, plan_id, task, step = _coordinator(
        tmp_path, workspace
    )
    coder = _Coder()
    controller = AgentController(
        planner=object(), coder=coder, verifier=object(), repair=object(),
        scheduler=object(), plan_store=stores.plan_store,
        step_store=stores.step_store, attempt_store=stores.attempt_store,
        attempt_coordinator=coordinator,
    )
    state = AgentState(
        request="build", phase=AgentPhase.EXECUTING, plan_id=plan_id,
        active_task_id=task.id, active_step_id=step.id,
    )

    original = authority.prepare_attempt
    authority.prepare_attempt = lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("database unavailable")
    )
    try:
        with pytest.raises(OSError, match="database unavailable"):
            controller.execute(state)
    finally:
        authority.prepare_attempt = original

    assert coder.calls == 0
    assert workspace.checkpoints == set()


def test_controller_refuses_writable_action_without_durable_coordinator(tmp_path):
    workspace = _Workspace()
    stores, _, _, _, _, plan_id, task, step = _coordinator(
        tmp_path, workspace
    )
    coder = _Coder()
    controller = AgentController(
        planner=object(), coder=coder, verifier=object(), repair=object(),
        scheduler=object(), plan_store=stores.plan_store,
        step_store=stores.step_store, attempt_store=stores.attempt_store,
    )
    state = AgentState(
        request="build", phase=AgentPhase.EXECUTING, plan_id=plan_id,
        active_task_id=task.id, active_step_id=step.id,
    )

    with pytest.raises(AgentControllerError, match="coordinator"):
        controller.execute(state)

    assert coder.calls == 0


def test_ready_attempt_has_durable_attempt_checkpoint_and_cursor(tmp_path):
    workspace = _Workspace()
    stores, runtime_store, authority, coordinator, run_id, plan_id, task, step = (
        _coordinator(tmp_path, workspace)
    )
    state = AgentState(
        request="build", phase=AgentPhase.EXECUTING, plan_id=plan_id,
        active_task_id=task.id, active_step_id=step.id,
    )

    coordinator.begin(state, task, step)

    run = runtime_store.get_run(run_id)
    journal = authority.get_journal_for_attempt(state.attempt_id)
    assert state.checkpoint_id in workspace.checkpoints
    assert journal["state"] == "READY"
    assert run["attempt_id"] == state.attempt_id
    assert run["checkpoint_id"] == state.checkpoint_id


def test_repair_budget_is_monotonic_in_database(tmp_path):
    workspace = _Workspace()
    _, _, authority, _, run_id, _, task, _ = _coordinator(tmp_path, workspace)

    assert authority.consume_budget(run_id, "TASK", task.id, limit=3) == 1
    assert authority.consume_budget(run_id, "TASK", task.id, limit=3) == 2
    assert authority.get_budget(run_id, "TASK", task.id) == {
        "consumed": 2,
        "limit": 3,
    }


def test_successful_attempt_commits_checkpoint_and_durable_record(tmp_path):
    workspace = _Workspace()
    stores, runtime_store, authority, coordinator, run_id, plan_id, task, step = (
        _coordinator(tmp_path, workspace)
    )
    state = AgentState(
        request="build", phase=AgentPhase.VERIFYING, plan_id=plan_id,
        active_task_id=task.id, active_step_id=step.id,
    )
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id

    coordinator.finish(state, ok=True, status="PASS")

    assert workspace.checkpoints == set()
    assert stores.attempt_store.get_attempt(attempt_id).status.value == "PASS"
    assert authority.get_journal_for_attempt(attempt_id)["state"] == "COMMITTED"
    run = runtime_store.get_run(run_id)
    assert run["attempt_id"] is None
    assert run["checkpoint_id"] is None


def test_rollback_failure_is_durable_and_propagates(tmp_path):
    workspace = _Workspace(rollback_error=OSError("restore denied"))
    stores, _, authority, coordinator, _, plan_id, task, step = _coordinator(
        tmp_path, workspace
    )
    state = AgentState(
        request="build", phase=AgentPhase.VERIFYING, plan_id=plan_id,
        active_task_id=task.id, active_step_id=step.id,
    )
    coordinator.begin(state, task, step)
    attempt_id = state.attempt_id

    with pytest.raises(RuntimeError, match="restore denied"):
        coordinator.finish(state, ok=False, status="FAIL", reason="tests failed")

    assert state.attempt_id == attempt_id
    assert stores.attempt_store.get_attempt(attempt_id).status.value == "IN_PROGRESS"
    journal = authority.get_journal_for_attempt(attempt_id)
    assert journal["state"] == "RECOVERY_FAILED"
    assert "restore denied" in journal["error"]
