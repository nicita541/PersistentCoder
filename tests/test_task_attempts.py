from __future__ import annotations

import pytest

from app.tasks.attempt_store import (
    AttemptStore,
    AttemptStoreError,
)
from app.tasks.models import (
    AttemptStatus,
    AttemptTargetType,
    PlanDraft,
    StepDraft,
    TaskDraft,
)
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore


def _create_task_and_step(
    database_path,
) -> tuple[int, int]:
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

    task = plan_store.get_tasks(
        plan_id
    )[0]

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

    return task.id, step.id


def test_attempt_status_contains_expected_states():
    assert (
        AttemptStatus.IN_PROGRESS.value
        == "IN_PROGRESS"
    )

    assert (
        AttemptStatus.PASS.value
        == "PASS"
    )

    assert (
        AttemptStatus.FAILED.value
        == "FAILED"
    )

    assert (
        AttemptStatus.BLOCKED.value
        == "BLOCKED"
    )


def test_attempt_target_types_exist():
    assert (
        AttemptTargetType.TASK.value
        == "TASK"
    )

    assert (
        AttemptTargetType.STEP.value
        == "STEP"
    )


def test_first_step_attempt_gets_number_one(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    attempt = store.start_step_attempt(
        step_id,
        approach="Использовать bcrypt.",
    )

    assert attempt.attempt_number == 1
    assert (
        attempt.target_type
        is AttemptTargetType.STEP
    )
    assert attempt.target_id == step_id
    assert (
        attempt.status
        is AttemptStatus.IN_PROGRESS
    )
    assert attempt.approach == (
        "Использовать bcrypt."
    )


def test_start_step_attempt_increments_step_counter(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    task_id, step_id = (
        _create_task_and_step(
            database_path
        )
    )

    store = AttemptStore(
        database_path=database_path
    )

    store.start_step_attempt(
        step_id
    )

    step_store = StepStore(
        database_path=database_path
    )

    step = step_store.get_step(
        step_id
    )

    assert step is not None
    assert step.attempt_count == 1


def test_failed_attempt_keeps_reason(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    attempt = store.start_step_attempt(
        step_id,
        approach="Использовать bcrypt.",
    )

    store.finish_attempt(
        attempt.id,
        status=AttemptStatus.FAILED,
        failure_reason=(
            "bcrypt import error"
        ),
        result_summary=(
            "Подход не сработал."
        ),
        result_artifacts=[],
    )

    reloaded = store.get_attempt(
        attempt.id
    )

    assert reloaded is not None

    assert (
        reloaded.status
        is AttemptStatus.FAILED
    )

    assert reloaded.failure_reason == (
        "bcrypt import error"
    )

    assert reloaded.result_summary == (
        "Подход не сработал."
    )


def test_second_attempt_does_not_destroy_first(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    first = store.start_step_attempt(
        step_id,
        approach="bcrypt",
    )

    store.finish_attempt(
        first.id,
        status=AttemptStatus.FAILED,
        failure_reason="import error",
    )

    second = store.start_step_attempt(
        step_id,
        approach="passlib",
    )

    assert second.attempt_number == 2

    attempts = store.get_step_attempts(
        step_id
    )

    assert len(attempts) == 2

    assert attempts[0].attempt_number == 1
    assert attempts[0].approach == "bcrypt"

    assert (
        attempts[0].status
        is AttemptStatus.FAILED
    )

    assert attempts[1].attempt_number == 2
    assert attempts[1].approach == "passlib"

    assert (
        attempts[1].status
        is AttemptStatus.IN_PROGRESS
    )


def test_attempt_history_survives_restart(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    first_store = AttemptStore(
        database_path=database_path
    )

    attempt = (
        first_store.start_step_attempt(
            step_id,
            approach="bcrypt",
        )
    )

    first_store.finish_attempt(
        attempt.id,
        status=AttemptStatus.FAILED,
        failure_reason="failed",
    )

    second_store = AttemptStore(
        database_path=database_path
    )

    attempts = (
        second_store.get_step_attempts(
            step_id
        )
    )

    assert len(attempts) == 1
    assert attempts[0].approach == "bcrypt"

    assert (
        attempts[0].status
        is AttemptStatus.FAILED
    )


def test_cannot_start_two_active_attempts_for_same_step(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    store.start_step_attempt(
        step_id
    )

    with pytest.raises(
        AttemptStoreError,
        match="active attempt",
    ):
        store.start_step_attempt(
            step_id
        )


def test_finished_attempt_is_immutable(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    attempt = store.start_step_attempt(
        step_id
    )

    store.finish_attempt(
        attempt.id,
        status=AttemptStatus.FAILED,
        failure_reason="first failure",
    )

    with pytest.raises(
        AttemptStoreError,
        match="already finished",
    ):
        store.finish_attempt(
            attempt.id,
            status=AttemptStatus.PASS,
            result_summary="fake success",
        )


def test_task_attempts_are_supported(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    task_id, _ = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    attempt = store.start_task_attempt(
        task_id,
        approach=(
            "Реализовать auth через JWT."
        ),
    )

    assert (
        attempt.target_type
        is AttemptTargetType.TASK
    )

    assert attempt.target_id == task_id
    assert attempt.attempt_number == 1

    plan_store = PlanStore(
        database_path=database_path
    )

    plan = plan_store.get_active_plan()

    assert plan is not None

    task = next(
        task
        for task in plan_store.get_tasks(
            plan.id
        )
        if task.id == task_id
    )

    assert task.attempt_count == 1


def test_pass_attempt_stores_artifacts(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    attempt = store.start_step_attempt(
        step_id,
        approach="Создать hashing service.",
    )

    store.finish_attempt(
        attempt.id,
        status=AttemptStatus.PASS,
        result_summary=(
            "Hashing service работает."
        ),
        result_artifacts=[
            "app/auth/passwords.py",
            "tests/test_passwords.py",
        ],
    )

    reloaded = store.get_attempt(
        attempt.id
    )

    assert reloaded is not None

    assert (
        reloaded.status
        is AttemptStatus.PASS
    )

    assert reloaded.result_artifacts == [
        "app/auth/passwords.py",
        "tests/test_passwords.py",
    ]


def test_failed_or_blocked_attempt_requires_reason(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, step_id = _create_task_and_step(
        database_path
    )

    store = AttemptStore(
        database_path=database_path
    )

    attempt = store.start_step_attempt(
        step_id
    )

    with pytest.raises(
        AttemptStoreError,
        match="failure reason",
    ):
        store.finish_attempt(
            attempt.id,
            status=AttemptStatus.FAILED,
        )