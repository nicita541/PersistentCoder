from __future__ import annotations

import pytest

from app.tasks.models import (
    PlanDraft,
    StepDraft,
    StepStatus,
    TaskDraft,
    TaskStatus,
    VerificationStatus,
    VerificationTargetType,
)
from app.tasks.scheduler import TaskScheduler
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.verification_store import (
    VerificationStore,
)
from app.tasks.verifier import (
    VerificationError,
    Verifier,
)


def _create_running_task(
    database_path,
) -> tuple[
    PlanStore,
    StepStore,
    VerificationStore,
    int,
]:
    plan_store = PlanStore(
        database_path=database_path
    )

    plan_id = plan_store.create_plan(
        PlanDraft(
            user_request=(
                "Добавить авторизацию."
            ),
            global_goal=(
                "Добавить авторизацию."
            ),
            tasks=[
                TaskDraft(
                    key="auth",
                    title="Создать auth",
                    description="Создать auth.",
                    success_criteria=[
                        "Авторизация работает",
                    ],
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
                description=(
                    "Создать password hashing."
                ),
                success_criteria=[
                    "Пароль хешируется",
                ],
            ),
            StepDraft(
                title="Создать token service",
                description=(
                    "Создать token service."
                ),
                success_criteria=[
                    "Token создаётся",
                ],
            ),
        ],
    )

    verification_store = (
        VerificationStore(
            database_path=database_path
        )
    )

    return (
        plan_store,
        step_store,
        verification_store,
        task.id,
    )


def _make_verifier(
    plan_store,
    step_store,
    verification_store,
):
    return Verifier(
        plan_store=plan_store,
        step_store=step_store,
        verification_store=(
            verification_store
        ),
    )


def test_verification_status_values():
    assert (
        VerificationStatus.PASS.value
        == "PASS"
    )

    assert (
        VerificationStatus.FAIL.value
        == "FAIL"
    )

    assert (
        VerificationStatus.BLOCKED.value
        == "BLOCKED"
    )


def test_verification_target_types():
    assert (
        VerificationTargetType.STEP.value
        == "STEP"
    )

    assert (
        VerificationTargetType.TASK.value
        == "TASK"
    )


def test_begin_step_verification_moves_step_to_verifying(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_step_verification(
        step.id
    )

    reloaded = step_store.get_step(
        step.id
    )

    assert reloaded is not None

    assert (
        reloaded.status
        is StepStatus.VERIFYING
    )


def test_pass_step_marks_step_done(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_step_verification(
        step.id
    )

    verifier.pass_step(
        step.id,
        evidence=[
            "4 tests passed",
        ],
    )

    reloaded = step_store.get_step(
        step.id
    )

    assert reloaded is not None

    assert (
        reloaded.status
        is StepStatus.DONE
    )

    assert (
        reloaded.verification_status
        == "PASS"
    )

    assert (
        reloaded.verification_evidence
        == [
            "4 tests passed",
        ]
    )


def test_fail_step_returns_to_in_progress(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_step_verification(
        step.id
    )

    verifier.fail_step(
        step.id,
        reason=(
            "Неверный пароль "
            "не отклоняется."
        ),
        evidence=[
            "test_invalid_password failed",
        ],
    )

    reloaded = step_store.get_step(
        step.id
    )

    assert reloaded is not None

    assert (
        reloaded.status
        is StepStatus.IN_PROGRESS
    )

    assert (
        reloaded.verification_status
        == "FAIL"
    )

    assert reloaded.failure_reason == (
        "Неверный пароль "
        "не отклоняется."
    )


def test_step_must_be_verifying_before_pass(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    with pytest.raises(
        VerificationError,
        match="VERIFYING",
    ):
        verifier.pass_step(
            step.id,
            evidence=[
                "fake pass",
            ],
        )


def test_verification_requires_evidence(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_step_verification(
        step.id
    )

    with pytest.raises(
        VerificationError,
        match="evidence",
    ):
        verifier.pass_step(
            step.id,
            evidence=[],
        )


def test_failed_then_passed_verifications_are_both_kept(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_step_verification(
        step.id
    )

    verifier.fail_step(
        step.id,
        reason="Первый подход неверен.",
        evidence=[
            "test failed",
        ],
    )

    verifier.begin_step_verification(
        step.id
    )

    verifier.pass_step(
        step.id,
        evidence=[
            "test passed",
        ],
    )

    history = (
        verification_store
        .get_step_verifications(
            step.id
        )
    )

    assert len(history) == 2

    assert (
        history[0].status
        is VerificationStatus.FAIL
    )

    assert (
        history[1].status
        is VerificationStatus.PASS
    )


def test_task_cannot_enter_verification_until_all_steps_done(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    with pytest.raises(
        VerificationError,
        match="all steps",
    ):
        verifier.begin_task_verification(
            task_id
        )


def test_all_steps_done_does_not_automatically_finish_task(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    for step in step_store.get_steps(
        task_id
    ):
        step_store.update_step_status(
            step.id,
            StepStatus.DONE,
        )

    task = plan_store.get_task(
        task_id
    )

    assert task is not None

    assert (
        task.status
        is TaskStatus.IN_PROGRESS
    )


def test_superseded_steps_do_not_block_task_verification(
    tmp_path,
):
    database_path = tmp_path / "planner.db"
    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(database_path)
    steps = step_store.get_steps(task_id)
    step_store.update_step_status(steps[0].id, StepStatus.SUPERSEDED)
    for step in steps[1:]:
        step_store.update_step_status(step.id, StepStatus.DONE)
    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_task_verification(task_id)

    assert plan_store.get_task(task_id).status is TaskStatus.VERIFYING


def test_pass_task_marks_task_done(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    for step in step_store.get_steps(
        task_id
    ):
        step_store.update_step_status(
            step.id,
            StepStatus.DONE,
        )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_task_verification(
        task_id
    )

    verifier.pass_task(
        task_id,
        evidence=[
            "auth integration passed",
            "12 tests passed",
        ],
    )

    task = plan_store.get_task(
        task_id
    )

    assert task is not None

    assert (
        task.status
        is TaskStatus.DONE
    )

    assert (
        task.verification_status
        == "PASS"
    )


def test_fail_task_returns_task_to_in_progress(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    for step in step_store.get_steps(
        task_id
    ):
        step_store.update_step_status(
            step.id,
            StepStatus.DONE,
        )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_task_verification(
        task_id
    )

    verifier.fail_task(
        task_id,
        reason=(
            "Integration test failed."
        ),
        evidence=[
            "test_auth_flow failed",
        ],
    )

    task = plan_store.get_task(
        task_id
    )

    assert task is not None

    assert (
        task.status
        is TaskStatus.IN_PROGRESS
    )

    assert (
        task.verification_status
        == "FAIL"
    )


def test_verification_history_survives_restart(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    (
        plan_store,
        step_store,
        verification_store,
        task_id,
    ) = _create_running_task(
        database_path
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    verifier = _make_verifier(
        plan_store,
        step_store,
        verification_store,
    )

    verifier.begin_step_verification(
        step.id
    )

    verifier.pass_step(
        step.id,
        evidence=[
            "verification passed",
        ],
    )

    restarted_store = VerificationStore(
        database_path=database_path
    )

    history = (
        restarted_store
        .get_step_verifications(
            step.id
        )
    )

    assert len(history) == 1

    assert (
        history[0].target_type
        is VerificationTargetType.STEP
    )

    assert (
        history[0].status
        is VerificationStatus.PASS
    )
