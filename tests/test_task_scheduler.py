from __future__ import annotations

from app.tasks.models import (
    PlanDraft,
    TaskDraft,
    TaskStatus,
)
from app.tasks.scheduler import TaskScheduler
from app.tasks.store import PlanStore


def _create_dependency_plan(
    store: PlanStore,
) -> int:
    plan = PlanDraft(
        user_request="Создать backend.",
        global_goal="Создать backend.",
        tasks=[
            TaskDraft(
                key="database",
                title="Настроить БД",
                description="Создать database layer.",
                priority=90,
                produces=[
                    "database connection",
                ],
            ),
            TaskDraft(
                key="models",
                title="Создать модели",
                description="Создать модели.",
                priority=80,
                requires=[
                    "database connection",
                ],
                produces=[
                    "User model",
                ],
                depends_on=[
                    "database",
                ],
            ),
            TaskDraft(
                key="api",
                title="Создать API",
                description="Создать API.",
                priority=70,
                requires=[
                    "User model",
                ],
                depends_on=[
                    "models",
                ],
            ),
        ],
    )

    return store.create_plan(plan)


def _task_by_key(
    store: PlanStore,
    plan_id: int,
    key: str,
):
    return next(
        task
        for task in store.get_tasks(plan_id)
        if task.key == key
    )


def test_root_task_becomes_ready(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan_id = _create_dependency_plan(
        store
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    database = _task_by_key(
        store,
        plan_id,
        "database",
    )

    models = _task_by_key(
        store,
        plan_id,
        "models",
    )

    api = _task_by_key(
        store,
        plan_id,
        "api",
    )

    assert (
        database.status
        is TaskStatus.READY
    )

    assert (
        models.status
        is TaskStatus.PENDING
    )

    assert (
        api.status
        is TaskStatus.PENDING
    )


def test_task_becomes_ready_after_dependencies_are_done(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan_id = _create_dependency_plan(
        store
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    database = _task_by_key(
        store,
        plan_id,
        "database",
    )

    store.update_task_status(
        database.id,
        TaskStatus.DONE,
    )

    scheduler.refresh(
        plan_id
    )

    models = _task_by_key(
        store,
        plan_id,
        "models",
    )

    api = _task_by_key(
        store,
        plan_id,
        "api",
    )

    assert (
        models.status
        is TaskStatus.READY
    )

    assert (
        api.status
        is TaskStatus.PENDING
    )


def test_failed_dependency_blocks_task(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan_id = _create_dependency_plan(
        store
    )

    database = _task_by_key(
        store,
        plan_id,
        "database",
    )

    store.update_task_status(
        database.id,
        TaskStatus.FAILED,
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    models = _task_by_key(
        store,
        plan_id,
        "models",
    )

    assert (
        models.status
        is TaskStatus.BLOCKED
    )


def test_blocked_dependency_blocks_task(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan_id = _create_dependency_plan(
        store
    )

    database = _task_by_key(
        store,
        plan_id,
        "database",
    )

    store.update_task_status(
        database.id,
        TaskStatus.BLOCKED,
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    models = _task_by_key(
        store,
        plan_id,
        "models",
    )

    assert (
        models.status
        is TaskStatus.BLOCKED
    )


def test_blocked_task_can_become_ready_later(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan_id = _create_dependency_plan(
        store
    )

    database = _task_by_key(
        store,
        plan_id,
        "database",
    )

    store.update_task_status(
        database.id,
        TaskStatus.BLOCKED,
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    models = _task_by_key(
        store,
        plan_id,
        "models",
    )

    assert (
        models.status
        is TaskStatus.BLOCKED
    )

    store.update_task_status(
        database.id,
        TaskStatus.DONE,
    )

    scheduler.refresh(
        plan_id
    )

    models = _task_by_key(
        store,
        plan_id,
        "models",
    )

    assert (
        models.status
        is TaskStatus.READY
    )


def test_selector_picks_highest_priority_ready_task(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan = PlanDraft(
        user_request="Сделать проект.",
        global_goal="Сделать проект.",
        tasks=[
            TaskDraft(
                key="low",
                title="Low priority",
                description="Low.",
                priority=10,
            ),
            TaskDraft(
                key="high",
                title="High priority",
                description="High.",
                priority=90,
            ),
            TaskDraft(
                key="medium",
                title="Medium priority",
                description="Medium.",
                priority=50,
            ),
        ],
    )

    plan_id = store.create_plan(
        plan
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    selected = scheduler.select_next(
        plan_id
    )

    assert selected is not None
    assert selected.key == "high"


def test_selector_uses_lowest_id_when_priority_is_equal(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan = PlanDraft(
        user_request="Сделать проект.",
        global_goal="Сделать проект.",
        tasks=[
            TaskDraft(
                key="first",
                title="Первая",
                description="Первая.",
                priority=50,
            ),
            TaskDraft(
                key="second",
                title="Вторая",
                description="Вторая.",
                priority=50,
            ),
        ],
    )

    plan_id = store.create_plan(
        plan
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    selected = scheduler.select_next(
        plan_id
    )

    assert selected is not None
    assert selected.key == "first"


def test_start_next_marks_selected_task_in_progress(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan_id = _create_dependency_plan(
        store
    )

    scheduler = TaskScheduler(store)

    started = scheduler.start_next(
        plan_id
    )

    assert started is not None
    assert started.key == "database"

    reloaded = _task_by_key(
        store,
        plan_id,
        "database",
    )

    assert (
        reloaded.status
        is TaskStatus.IN_PROGRESS
    )


def test_only_one_task_can_be_active(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan = PlanDraft(
        user_request="Сделать проект.",
        global_goal="Сделать проект.",
        tasks=[
            TaskDraft(
                key="first",
                title="Первая",
                description="Первая.",
                priority=90,
            ),
            TaskDraft(
                key="second",
                title="Вторая",
                description="Вторая.",
                priority=80,
            ),
        ],
    )

    plan_id = store.create_plan(
        plan
    )

    scheduler = TaskScheduler(store)

    first = scheduler.start_next(
        plan_id
    )

    assert first is not None
    assert first.key == "first"

    second = scheduler.start_next(
        plan_id
    )

    assert second is None

    tasks = store.get_tasks(
        plan_id
    )

    active_tasks = [
        task
        for task in tasks
        if task.status
        in {
            TaskStatus.IN_PROGRESS,
            TaskStatus.VERIFYING,
        }
    ]

    assert len(active_tasks) == 1


def test_verifying_task_prevents_starting_another_task(
    tmp_path,
):
    store = PlanStore(
        database_path=(
            tmp_path / "planner.db"
        )
    )

    plan = PlanDraft(
        user_request="Сделать проект.",
        global_goal="Сделать проект.",
        tasks=[
            TaskDraft(
                key="first",
                title="Первая",
                description="Первая.",
                priority=90,
            ),
            TaskDraft(
                key="second",
                title="Вторая",
                description="Вторая.",
                priority=80,
            ),
        ],
    )

    plan_id = store.create_plan(
        plan
    )

    scheduler = TaskScheduler(store)

    scheduler.refresh(
        plan_id
    )

    first = _task_by_key(
        store,
        plan_id,
        "first",
    )

    store.update_task_status(
        first.id,
        TaskStatus.VERIFYING,
    )

    selected = scheduler.start_next(
        plan_id
    )

    assert selected is None