from app.tasks.models import (
    PlanDraft,
    PlanRecord,
    PlanStatus,
    TaskDraft,
    TaskRecord,
    TaskStatus,
)


def test_task_status_contains_expected_states():
    assert TaskStatus.PENDING.value == "PENDING"
    assert TaskStatus.READY.value == "READY"
    assert TaskStatus.IN_PROGRESS.value == "IN_PROGRESS"
    assert TaskStatus.VERIFYING.value == "VERIFYING"
    assert TaskStatus.DONE.value == "DONE"
    assert TaskStatus.BLOCKED.value == "BLOCKED"
    assert TaskStatus.FAILED.value == "FAILED"
    assert TaskStatus.SUPERSEDED.value == "SUPERSEDED"


def test_plan_status_contains_expected_states():
    assert PlanStatus.ACTIVE.value == "ACTIVE"
    assert PlanStatus.DONE.value == "DONE"
    assert PlanStatus.SUPERSEDED.value == "SUPERSEDED"
    assert PlanStatus.FAILED.value == "FAILED"


def test_task_draft_has_safe_defaults():
    task = TaskDraft(
        title="Настроить базу данных",
        description="Подготовить подключение к БД.",
    )

    assert task.title == "Настроить базу данных"
    assert task.description == "Подготовить подключение к БД."

    assert task.priority == 50
    assert task.parent_id is None

    assert task.requires == []
    assert task.produces == []
    assert task.success_criteria == []


def test_task_draft_lists_are_not_shared():
    first = TaskDraft(
        title="Первая",
        description="Первая задача.",
    )

    second = TaskDraft(
        title="Вторая",
        description="Вторая задача.",
    )

    first.requires.append("database")

    assert first.requires == ["database"]
    assert second.requires == []


def test_task_draft_can_store_contract():
    task = TaskDraft(
        title="Создать модели",
        description="Создать модели User и Note.",
        priority=80,
        requires=[
            "database connection",
        ],
        produces=[
            "User model",
            "Note model",
        ],
        success_criteria=[
            "User model существует",
            "Note model существует",
            "Note связана с User",
        ],
    )

    assert task.priority == 80

    assert task.requires == [
        "database connection",
    ]

    assert task.produces == [
        "User model",
        "Note model",
    ]

    assert task.success_criteria == [
        "User model существует",
        "Note model существует",
        "Note связана с User",
    ]


def test_plan_draft_contains_goal_and_tasks():
    tasks = [
        TaskDraft(
            title="Настроить БД",
            description="Подготовить database layer.",
        ),
        TaskDraft(
            title="Создать модели",
            description="Создать модели данных.",
        ),
    ]

    plan = PlanDraft(
        user_request=(
            "Сделай backend для заметок."
        ),
        global_goal=(
            "Создать backend для заметок."
        ),
        tasks=tasks,
    )

    assert plan.user_request == (
        "Сделай backend для заметок."
    )

    assert plan.global_goal == (
        "Создать backend для заметок."
    )

    assert len(plan.tasks) == 2


def test_task_record_represents_persisted_task():
    task = TaskRecord(
        id=7,
        plan_id=2,
        parent_id=None,
        title="Создать модели",
        description="Создать User и Note.",
        status=TaskStatus.PENDING,
        priority=80,
        requires=["database connection"],
        produces=["User model", "Note model"],
        success_criteria=[
            "User model существует",
        ],
        current_step=None,
        attempt_count=0,
        result_summary=None,
        result_artifacts=[],
        verification_status=None,
        verification_evidence=[],
        created_at="2026-09-11 10:00:00",
        started_at=None,
        finished_at=None,
        updated_at="2026-09-11 10:00:00",
    )

    assert task.id == 7
    assert task.plan_id == 2
    assert task.status is TaskStatus.PENDING
    assert task.attempt_count == 0
    assert task.result_summary is None


def test_plan_record_represents_persisted_plan():
    plan = PlanRecord(
        id=3,
        version=1,
        user_request="Сделай backend.",
        global_goal="Создать backend.",
        status=PlanStatus.ACTIVE,
        created_at="2026-09-11 10:00:00",
        updated_at="2026-09-11 10:00:00",
    )

    assert plan.id == 3
    assert plan.version == 1
    assert plan.status is PlanStatus.ACTIVE

def test_task_draft_supports_stable_key_and_dependencies():
    task = TaskDraft(
        key="migrations",
        title="Создать миграции",
        description="Создать migrations.",
        depends_on=[
            "database",
            "models",
        ],
    )

    assert task.key == "migrations"

    assert task.depends_on == [
        "database",
        "models",
    ]


def test_task_dependency_lists_are_not_shared():
    first = TaskDraft(
        key="first",
        title="Первая",
        description="Первая.",
    )

    second = TaskDraft(
        key="second",
        title="Вторая",
        description="Вторая.",
    )

    first.depends_on.append(
        "database"
    )

    assert first.depends_on == [
        "database",
    ]

    assert second.depends_on == []