from __future__ import annotations

from app.agent.repair.agent import RepairAgent
from app.agent.repair.analyzer import FailureAnalyzer
from app.agent.repair.strategies import (
    GIVE_UP,
    REPLAN_TASK,
    RETRY_STEP,
    RepairStrategySelector,
)
from app.agent.state import VerificationResult
from app.tasks.models import (
    AttemptStatus,
    ReplanTargetType,
    TaskStatus,
)
from app.tasks.replanner import Replanner

from helpers import make_stores, seed_plan


def _agent(
    stores,
    *,
    max_step_attempts: int = 2,
    max_task_attempts: int = 3,
):
    replanner = Replanner(
        plan_store=stores.plan_store,
        step_store=stores.step_store,
        attempt_store=stores.attempt_store,
        replan_store=stores.replan_store,
        max_step_attempts=max_step_attempts,
    )

    return RepairAgent(
        analyzer=FailureAnalyzer(),
        strategies=RepairStrategySelector(
            max_step_attempts=max_step_attempts,
            max_task_attempts=max_task_attempts,
        ),
        replanner=replanner,
        attempt_store=stores.attempt_store,
        replan_store=stores.replan_store,
    )


def _failure() -> VerificationResult:
    return VerificationResult(
        ok=False,
        status="FAIL",
        reason="pytest failed",
        evidence=["command 'pytest' -> rc=1"],
    )


def _prepare(tmp_path):
    stores = make_stores(tmp_path)

    _, task, step = seed_plan(stores)

    stores.plan_store.update_task_status(
        task.id,
        TaskStatus.IN_PROGRESS,
    )

    return stores, task.id, step.id


def test_first_failure_retries_step(tmp_path):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    outcome = agent.repair(
        task=stores.plan_store.get_task(task_id),
        verification=_failure(),
        step=stores.step_store.get_step(step_id),
        step_attempt_number=1,
        task_attempt_number=1,
    )

    assert outcome.action == RETRY_STEP
    assert outcome.scope == "STEP"

    records = (
        stores.replan_store.get_for_target(
            ReplanTargetType.STEP,
            step_id,
        )
    )

    assert records, (
        "Replanner must record a step replan"
    )


def test_escalates_to_task_replan(tmp_path):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    outcome = agent.repair(
        task=stores.plan_store.get_task(task_id),
        verification=_failure(),
        step=stores.step_store.get_step(step_id),
        step_attempt_number=2,
        task_attempt_number=2,
    )

    assert outcome.action == REPLAN_TASK
    assert outcome.scope == "TASK"

    records = (
        stores.replan_store.get_for_target(
            ReplanTargetType.TASK,
            task_id,
        )
    )

    assert records


def test_gives_up_after_task_attempt_limit(
    tmp_path,
):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    outcome = agent.repair(
        task=stores.plan_store.get_task(task_id),
        verification=_failure(),
        step=stores.step_store.get_step(step_id),
        step_attempt_number=2,
        task_attempt_number=3,
    )

    assert outcome.action == GIVE_UP


def test_analyzer_prefers_task_scope_on_blocked(
    tmp_path,
):
    stores, task_id, _ = _prepare(tmp_path)

    analysis = FailureAnalyzer().analyze(
        task=stores.plan_store.get_task(task_id),
        verification=VerificationResult(
            ok=False,
            status="BLOCKED",
            reason="dependency failed",
            evidence=["dependency"],
        ),
    )

    assert analysis.scope == "TASK"


# ==========================================
# STRUCTURED APPROACH / NO REPEAT
# ==========================================


def test_repair_returns_structured_approach(tmp_path):
    stores, task_id, step_id = _prepare(tmp_path)

    agent = _agent(stores)

    outcome = agent.repair(
        task=stores.plan_store.get_task(task_id),
        verification=_failure(),
        step=stores.step_store.get_step(step_id),
        step_attempt_number=1,
        task_attempt_number=1,
    )

    plan = outcome.approach

    assert plan is not None
    assert plan.scope in ("STEP", "TASK")
    assert plan.failure_class
    assert plan.root_cause
    assert plan.new_approach
    assert isinstance(plan.files_to_inspect, list)
    assert isinstance(plan.verification_plan, list)


def test_no_repeating_failed_approach(tmp_path):
    stores, task_id, step_id = _prepare(tmp_path)

    # A previous attempt already FAILED with exactly this approach.
    record = stores.attempt_store.start_step_attempt(
        step_id,
        approach="VERIFICATION_FAIL::pytest failed",
    )
    stores.attempt_store.finish_attempt(
        record.id,
        status=AttemptStatus.FAILED,
        failure_reason="pytest failed",
    )

    agent = _agent(stores)

    outcome = agent.repair(
        task=stores.plan_store.get_task(task_id),
        verification=_failure(),
        step=stores.step_store.get_step(step_id),
        step_attempt_number=1,
        task_attempt_number=1,
    )

    # The same approach must not be retried: escalate.
    assert outcome.action == REPLAN_TASK
    assert outcome.approach is not None
    assert outcome.approach.root_cause == "pytest failed"

