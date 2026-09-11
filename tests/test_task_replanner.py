from __future__ import annotations

import pytest

from app.tasks.attempt_store import AttemptStore
from app.tasks.models import (
    AttemptStatus,
    PlanDraft,
    ReplanScope,
    ReplanTargetType,
    ReplanTrigger,
    StepDraft,
    TaskDraft,
    TaskStatus,
)
from app.tasks.replan_store import ReplanStore
from app.tasks.replanner import (
    ReplanError,
    Replanner,
)
from app.tasks.scheduler import TaskScheduler
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore


def _create_environment(
    database_path,
):
    plan_store = PlanStore(
        database_path=database_path
    )

    plan_id = plan_store.create_plan(
        PlanDraft(
            user_request="Добавить авторизацию.",
            global_goal="Добавить авторизацию.",
            tasks=[
                TaskDraft(
                    key="auth",
                    title="Создать auth",
                    description="Создать auth.",
                )
            ],
        )
    )

    scheduler = TaskScheduler(
        plan_store
    )

    task = scheduler.start_next(
        plan_id
    )

    assert task is not None

    step_store = StepStore(
        database_path=database_path
    )

    step_store.create_steps(
        task.id,
        [
            StepDraft(
                title="Создать hashing",
                description="Создать hashing.",
            )
        ],
    )

    step = step_store.get_steps(
        task.id
    )[0]

    attempt_store = AttemptStore(
        database_path=database_path
    )

    replan_store = ReplanStore(
        database_path=database_path
    )

    replanner = Replanner(
        plan_store=plan_store,
        step_store=step_store,
        attempt_store=attempt_store,
        replan_store=replan_store,
    )

    return (
        plan_store,
        step_store,
        attempt_store,
        replan_store,
        replanner,
        plan_id,
        task.id,
        step.id,
    )


def _add_failed_attempt(
    attempt_store: AttemptStore,
    step_id: int,
    approach: str,
) -> None:
    attempt = (
        attempt_store.start_step_attempt(
            step_id,
            approach=approach,
        )
    )

    attempt_store.finish_attempt(
        attempt.id,
        status=AttemptStatus.FAILED,
        failure_reason="approach failed",
    )


def test_replan_scopes_exist():
    assert ReplanScope.ACTION.value == "ACTION"
    assert ReplanScope.STEP.value == "STEP"
    assert ReplanScope.TASK.value == "TASK"
    assert ReplanScope.GLOBAL.value == "GLOBAL"


def test_replan_triggers_exist():
    assert (
        ReplanTrigger.VERIFICATION_FAIL.value
        == "VERIFICATION_FAIL"
    )

    assert (
        ReplanTrigger.ATTEMPT_LIMIT.value
        == "ATTEMPT_LIMIT"
    )

    assert (
        ReplanTrigger.DEPENDENCY_FAILURE.value
        == "DEPENDENCY_FAILURE"
    )

    assert (
        ReplanTrigger.USER_CHANGE.value
        == "USER_CHANGE"
    )

    assert (
        ReplanTrigger.INVALID_ASSUMPTION.value
        == "INVALID_ASSUMPTION"
    )


def test_first_failed_step_attempt_replans_only_step(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        attempt_store,
        _,
        replanner,
        _,
        task_id,
        step_id,
    ) = _create_environment(
        database_path
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "bcrypt",
    )

    decision = (
        replanner.decide_step_failure(
            step_id,
            reason="Verification failed.",
        )
    )

    assert (
        decision.scope
        is ReplanScope.STEP
    )

    assert (
        decision.trigger
        is ReplanTrigger.VERIFICATION_FAIL
    )

    assert (
        decision.target_type
        is ReplanTargetType.STEP
    )

    assert decision.target_id == step_id

    assert (
            decision.target_type
            is ReplanTargetType.STEP
    )


def test_third_failed_step_attempt_escalates_to_task(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        attempt_store,
        _,
        replanner,
        _,
        task_id,
        step_id,
    ) = _create_environment(
        database_path
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "bcrypt",
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "passlib",
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "argon2",
    )

    decision = (
        replanner.decide_step_failure(
            step_id,
            reason=(
                "Три подхода не сработали."
            ),
        )
    )

    assert (
        decision.scope
        is ReplanScope.TASK
    )

    assert (
        decision.trigger
        is ReplanTrigger.ATTEMPT_LIMIT
    )

    assert (
        decision.target_type
        is ReplanTargetType.TASK
    )

    assert decision.target_id == task_id


def test_failed_approach_cannot_be_repeated(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        attempt_store,
        _,
        replanner,
        _,
        _,
        step_id,
    ) = _create_environment(
        database_path
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "bcrypt with salt",
    )

    with pytest.raises(
        ReplanError,
        match="already failed",
    ):
        replanner.assert_step_approach_allowed(
            step_id,
            "BCRYPT   WITH SALT",
        )


def test_different_approach_is_allowed(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        attempt_store,
        _,
        replanner,
        _,
        _,
        step_id,
    ) = _create_environment(
        database_path
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "bcrypt",
    )

    replanner.assert_step_approach_allowed(
        step_id,
        "argon2",
    )


def test_task_verification_failure_replans_task(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        _,
        _,
        replanner,
        _,
        task_id,
        _,
    ) = _create_environment(
        database_path
    )

    decision = (
        replanner.decide_task_failure(
            task_id,
            reason=(
                "Integration verification failed."
            ),
        )
    )

    assert (
        decision.scope
        is ReplanScope.TASK
    )

    assert (
        decision.trigger
        is ReplanTrigger.VERIFICATION_FAIL
    )

    assert (
        decision.target_type
        is ReplanTargetType.TASK
    )

    assert decision.target_id == task_id


def test_dependency_failure_replans_affected_task(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        _,
        _,
        replanner,
        _,
        task_id,
        _,
    ) = _create_environment(
        database_path
    )

    decision = (
        replanner.decide_dependency_failure(
            task_id,
            reason=(
                "Required database task failed."
            ),
        )
    )

    assert (
        decision.scope
        is ReplanScope.TASK
    )

    assert (
        decision.trigger
        is ReplanTrigger.DEPENDENCY_FAILURE
    )


def test_user_change_causes_global_replan(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        _,
        _,
        replanner,
        plan_id,
        _,
        _,
    ) = _create_environment(
        database_path
    )

    decision = (
        replanner.decide_global_replan(
            plan_id,
            reason=(
                "Пользователь изменил "
                "требование к архитектуре."
            ),
            trigger=(
                ReplanTrigger.USER_CHANGE
            ),
        )
    )

    assert (
        decision.scope
        is ReplanScope.GLOBAL
    )

    assert (
        decision.target_type
        is ReplanTargetType.PLAN
    )

    assert decision.target_id == plan_id


def test_replan_history_is_append_only(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        attempt_store,
        replan_store,
        replanner,
        _,
        _,
        step_id,
    ) = _create_environment(
        database_path
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "bcrypt",
    )

    replanner.decide_step_failure(
        step_id,
        reason="First failure.",
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "passlib",
    )

    replanner.decide_step_failure(
        step_id,
        reason="Second failure.",
    )

    history = (
        replan_store.get_for_target(
            ReplanTargetType.STEP,
            step_id,
        )
    )

    assert len(history) == 2

    assert history[0].reason == (
        "First failure."
    )

    assert history[1].reason == (
        "Second failure."
    )


def test_replan_history_survives_restart(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        _,
        _,
        attempt_store,
        _,
        replanner,
        _,
        _,
        step_id,
    ) = _create_environment(
        database_path
    )

    _add_failed_attempt(
        attempt_store,
        step_id,
        "bcrypt",
    )

    replanner.decide_step_failure(
        step_id,
        reason="Failed.",
    )

    restarted_store = ReplanStore(
        database_path=database_path
    )

    history = (
        restarted_store.get_for_target(
            ReplanTargetType.STEP,
            step_id,
        )
    )

    assert len(history) == 1
    assert history[0].reason == "Failed."


def test_done_task_is_not_reopened_by_replanner(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        _,
        _,
        _,
        replanner,
        _,
        task_id,
        _,
    ) = _create_environment(
        database_path
    )

    plan_store.update_task_status(
        task_id,
        TaskStatus.DONE,
    )

    with pytest.raises(
        ReplanError,
        match="DONE",
    ):
        replanner.decide_task_failure(
            task_id,
            reason="Try again.",
        )