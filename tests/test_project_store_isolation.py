from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.tasks.attempt_store import AttemptStore
from app.tasks.models import (
    PlanDraft,
    PlanStatus,
    ReplanDecision,
    ReplanScope,
    ReplanTargetType,
    ReplanTrigger,
    StepDraft,
    TaskDraft,
    TaskStatus,
    VerificationStatus,
)
from app.tasks.replan_store import ReplanStore
from app.tasks.runtime_store import RuntimeStore
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.store_context import StoreContext
from app.tasks.verification_store import VerificationStore


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


def _plan(goal: str) -> PlanDraft:
    return PlanDraft(
        user_request=goal,
        global_goal=goal,
        tasks=[
            TaskDraft(
                key="write-file",
                title="Write file",
                description="Create one file",
                produces=["result.txt"],
                success_criteria=["result.txt exists"],
            )
        ],
    )


def _seed_step(database: Path, context: StoreContext) -> int:
    plan_store = PlanStore(context)
    plan_id = plan_store.create_plan(_plan("Seed"))
    task_id = plan_store.get_tasks(plan_id)[0].id
    return StepStore(context).create_steps(
        task_id,
        [
            StepDraft(
                title="Write result",
                description="Create result.txt",
                produces=["result.txt"],
                success_criteria=["result.txt exists"],
            )
        ],
    )[0]


def test_runtime_store_recovers_only_matching_project(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")

    store_a = RuntimeStore(context_a)
    run_id = store_a.start_run(
        request="project A",
        sandbox_session_id="session-a",
    )

    assert RuntimeStore(context_b).recover_interrupted() == []

    run = store_a.get_run(run_id)
    assert run is not None
    assert run["project_id"] == "a" * 64
    assert run["canonical_source_root"] == str(
        (tmp_path / "a").resolve()
    )
    assert run["status"] == "RUNNING"


def test_bound_runtime_store_cannot_read_another_project_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    run_id = RuntimeStore(context_a).start_run("project A")

    assert RuntimeStore(context_b).get_run(run_id) is None


def test_active_plan_is_project_scoped(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    store_a = PlanStore(context_a)
    store_b = PlanStore(context_b)

    plan_id = store_a.create_plan(_plan("Project A"))

    active_a = store_a.get_active_plan()
    assert active_a is not None
    task_id = store_a.get_tasks(plan_id)[0].id
    assert active_a.id == plan_id
    assert active_a.project_id == "a" * 64
    assert store_b.get_active_plan() is None
    assert store_b.get_plan(plan_id) is None
    assert store_b.get_tasks(plan_id) == []

    with pytest.raises(ValueError):
        store_b.set_plan_status(plan_id, PlanStatus.FAILED)

    with pytest.raises(ValueError):
        store_b.update_task_status(task_id, TaskStatus.FAILED)

    assert store_b.get_task_dependencies(task_id) == []

    assert store_a.get_active_plan() is not None


def test_legacy_unscoped_rows_are_not_adopted_by_bound_store(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RUNNING',
                phase TEXT,
                plan_id INTEGER,
                task_id INTEGER,
                step_id INTEGER,
                attempt_id INTEGER,
                sandbox_session_id TEXT,
                checkpoint_id TEXT,
                recovery_note TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            "INSERT INTO agent_runs (request) VALUES ('legacy')"
        )

    context = _context(database, "a", tmp_path / "a")
    store = RuntimeStore(context)

    assert store.get_running_runs() == []
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT project_id FROM agent_runs WHERE request = 'legacy'"
        ).fetchone()
    assert row == (None,)


def test_step_store_rejects_another_projects_task_and_step(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    step_store_a = StepStore(context_a)
    step_store_b = StepStore(context_b)
    step_id = _seed_step(database, context_a)

    assert step_store_a.get_step(step_id) is not None
    assert step_store_b.get_step(step_id) is None


def test_attempt_store_rejects_another_projects_attempt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    step_id = _seed_step(database, context_a)

    attempt = AttemptStore(context_a).start_step_attempt(step_id)

    assert AttemptStore(context_a).get_attempt(attempt.id) is not None
    assert AttemptStore(context_b).get_attempt(attempt.id) is None


def test_verification_store_rejects_another_projects_records(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    step_id = _seed_step(database, context_a)

    VerificationStore(context_a).record_step(
        step_id,
        status=VerificationStatus.PASS,
        evidence=["compiled"],
    )

    assert len(
        VerificationStore(context_a).get_step_verifications(step_id)
    ) == 1
    assert (
        VerificationStore(context_b).get_step_verifications(step_id)
        == []
    )


def test_replan_store_rejects_another_projects_records(
    tmp_path: Path,
) -> None:
    database = tmp_path / "shared.db"
    context_a = _context(database, "a", tmp_path / "a")
    context_b = _context(database, "b", tmp_path / "b")
    step_id = _seed_step(database, context_a)
    decision = ReplanDecision(
        scope=ReplanScope.STEP,
        trigger=ReplanTrigger.VERIFICATION_FAIL,
        target_type=ReplanTargetType.STEP,
        target_id=step_id,
        reason="compile failed",
    )

    ReplanStore(context_a).record(decision)

    assert len(
        ReplanStore(context_a).get_for_target(
            ReplanTargetType.STEP,
            step_id,
        )
    ) == 1
    assert ReplanStore(context_b).get_for_target(
        ReplanTargetType.STEP,
        step_id,
    ) == []
