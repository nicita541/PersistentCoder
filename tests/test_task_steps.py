from __future__ import annotations

import pytest

from app.tasks.models import (
    PlanDraft,
    StepDraft,
    StepStatus,
    TaskDraft,
)
from app.tasks.step_store import (
    StepStore,
    StepStoreError,
)
from app.tasks.store import PlanStore


def _create_task(
    database_path,
) -> tuple[PlanStore, int]:
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
                    title="Создать авторизацию",
                    description="Реализовать auth.",
                )
            ],
        )
    )

    task = plan_store.get_tasks(
        plan_id
    )[0]

    return plan_store, task.id


def _make_steps() -> list[StepDraft]:
    return [
        StepDraft(
            title="Создать password hashing",
            description=(
                "Добавить безопасное "
                "хеширование паролей."
            ),
            produces=[
                "password hashing service",
            ],
            success_criteria=[
                "Пароль хешируется",
                "Пароль можно проверить",
            ],
        ),
        StepDraft(
            title="Создать token service",
            description=(
                "Добавить создание "
                "и проверку токенов."
            ),
            requires=[
                "password hashing service",
            ],
            produces=[
                "token service",
            ],
            success_criteria=[
                "Токен создаётся",
                "Токен проверяется",
            ],
        ),
        StepDraft(
            title="Создать login endpoint",
            description=(
                "Добавить endpoint входа."
            ),
            requires=[
                "token service",
            ],
            produces=[
                "login endpoint",
            ],
            success_criteria=[
                "Верные данные дают token",
                "Неверный пароль отклоняется",
            ],
        ),
    ]


def test_step_draft_has_safe_defaults():
    step = StepDraft(
        title="Создать сервис",
        description="Создать сервис.",
    )

    assert step.requires == []
    assert step.produces == []
    assert step.success_criteria == []


def test_step_draft_lists_are_not_shared():
    first = StepDraft(
        title="Первый",
        description="Первый.",
    )

    second = StepDraft(
        title="Второй",
        description="Второй.",
    )

    first.requires.append(
        "database"
    )

    assert first.requires == [
        "database"
    ]

    assert second.requires == []


def test_step_status_contains_expected_states():
    assert StepStatus.PENDING.value == "PENDING"
    assert StepStatus.READY.value == "READY"
    assert StepStatus.IN_PROGRESS.value == "IN_PROGRESS"
    assert StepStatus.VERIFYING.value == "VERIFYING"
    assert StepStatus.DONE.value == "DONE"
    assert StepStatus.BLOCKED.value == "BLOCKED"
    assert StepStatus.FAILED.value == "FAILED"
    assert StepStatus.SUPERSEDED.value == "SUPERSEDED"


def test_steps_survive_database_round_trip(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, task_id = _create_task(
        database_path
    )

    step_store = StepStore(
        database_path=database_path
    )

    step_store.create_steps(
        task_id,
        _make_steps(),
    )

    steps = step_store.get_steps(
        task_id
    )

    assert len(steps) == 3

    assert steps[0].position == 1
    assert steps[1].position == 2
    assert steps[2].position == 3

    assert steps[0].title == (
        "Создать password hashing"
    )

    assert steps[1].requires == [
        "password hashing service",
    ]

    assert steps[2].produces == [
        "login endpoint",
    ]


def test_new_steps_are_pending(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, task_id = _create_task(
        database_path
    )

    step_store = StepStore(
        database_path=database_path
    )

    step_store.create_steps(
        task_id,
        _make_steps(),
    )

    steps = step_store.get_steps(
        task_id
    )

    assert all(
        step.status
        is StepStatus.PENDING
        for step in steps
    )


def test_steps_survive_store_restart(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, task_id = _create_task(
        database_path
    )

    first_store = StepStore(
        database_path=database_path
    )

    first_store.create_steps(
        task_id,
        _make_steps(),
    )

    second_store = StepStore(
        database_path=database_path
    )

    steps = second_store.get_steps(
        task_id
    )

    assert len(steps) == 3
    assert steps[1].position == 2


def test_current_step_is_saved_on_task(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    plan_store, task_id = _create_task(
        database_path
    )

    step_store = StepStore(
        database_path=database_path
    )

    step_store.create_steps(
        task_id,
        _make_steps(),
    )

    first_step = step_store.get_steps(
        task_id
    )[0]

    step_store.set_current_step(
        task_id=task_id,
        step_id=first_step.id,
    )

    task = next(
        task
        for task in plan_store.get_tasks(
            plan_store.get_active_plan().id
        )
        if task.id == task_id
    )

    assert (
        task.current_step
        == first_step.id
    )


def test_step_status_survives_restart(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, task_id = _create_task(
        database_path
    )

    first_store = StepStore(
        database_path=database_path
    )

    first_store.create_steps(
        task_id,
        _make_steps(),
    )

    first_step = first_store.get_steps(
        task_id
    )[0]

    first_store.update_step_status(
        first_step.id,
        StepStatus.IN_PROGRESS,
    )

    second_store = StepStore(
        database_path=database_path
    )

    reloaded = second_store.get_step(
        first_step.id
    )

    assert reloaded is not None

    assert (
        reloaded.status
        is StepStatus.IN_PROGRESS
    )


def test_step_result_is_persisted(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, task_id = _create_task(
        database_path
    )

    step_store = StepStore(
        database_path=database_path
    )

    step_store.create_steps(
        task_id,
        _make_steps(),
    )

    step = step_store.get_steps(
        task_id
    )[0]

    step_store.set_step_result(
        step.id,
        result_summary=(
            "Password hashing создан."
        ),
        result_artifacts=[
            "app/auth/passwords.py",
        ],
        verification_status="PASS",
        verification_evidence=[
            "4 tests passed",
        ],
    )

    reloaded = step_store.get_step(
        step.id
    )

    assert reloaded is not None

    assert reloaded.result_summary == (
        "Password hashing создан."
    )

    assert reloaded.result_artifacts == [
        "app/auth/passwords.py",
    ]

    assert (
        reloaded.verification_status
        == "PASS"
    )

    assert reloaded.verification_evidence == [
        "4 tests passed",
    ]


def test_steps_cannot_be_created_twice_for_same_task(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    _, task_id = _create_task(
        database_path
    )

    step_store = StepStore(
        database_path=database_path
    )

    step_store.create_steps(
        task_id,
        _make_steps(),
    )

    with pytest.raises(
        StepStoreError,
        match="already has steps",
    ):
        step_store.create_steps(
            task_id,
            _make_steps(),
        )