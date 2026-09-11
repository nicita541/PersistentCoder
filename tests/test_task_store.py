from __future__ import annotations

import sqlite3

from app.tasks.models import (
    PlanDraft,
    PlanStatus,
    TaskDraft,
    TaskStatus,
)
from app.tasks.store import PlanStore


def _make_plan() -> PlanDraft:
    return PlanDraft(
        user_request=(
            "Сделай backend для заметок."
        ),
        global_goal=(
            "Создать backend для заметок."
        ),
        tasks=[
            TaskDraft(
                title="Настроить базу данных",
                description=(
                    "Подготовить database layer."
                ),
                priority=90,
                requires=[],
                produces=[
                    "database connection",
                ],
                success_criteria=[
                    "Подключение к БД работает",
                ],
            ),
            TaskDraft(
                title="Создать модели",
                description=(
                    "Создать User и Note."
                ),
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
                ],
            ),
        ],
    )


def test_new_store_has_no_active_plan(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    assert store.get_active_plan() is None


def test_create_plan_returns_plan_id(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        _make_plan()
    )

    assert isinstance(plan_id, int)
    assert plan_id > 0


def test_created_plan_can_be_loaded(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        _make_plan()
    )

    plan = store.get_plan(
        plan_id
    )

    assert plan is not None

    assert plan.id == plan_id
    assert plan.version == 1

    assert plan.user_request == (
        "Сделай backend для заметок."
    )

    assert plan.global_goal == (
        "Создать backend для заметок."
    )

    assert plan.status is PlanStatus.ACTIVE


def test_plan_tasks_are_created_as_pending(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        _make_plan()
    )

    tasks = store.get_tasks(
        plan_id
    )

    assert len(tasks) == 2

    assert tasks[0].title == (
        "Настроить базу данных"
    )

    assert (
        tasks[0].status
        is TaskStatus.PENDING
    )

    assert (
        tasks[1].status
        is TaskStatus.PENDING
    )


def test_task_contract_survives_database_round_trip(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        _make_plan()
    )

    tasks = store.get_tasks(
        plan_id
    )

    database_task = tasks[0]
    models_task = tasks[1]

    assert database_task.produces == [
        "database connection",
    ]

    assert models_task.requires == [
        "database connection",
    ]

    assert models_task.produces == [
        "User model",
        "Note model",
    ]

    assert (
        models_task.success_criteria
        == [
            "User model существует",
            "Note model существует",
        ]
    )


def test_active_plan_survives_store_restart(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    first_store = PlanStore(
        database_path=database_path
    )

    plan_id = first_store.create_plan(
        _make_plan()
    )

    second_store = PlanStore(
        database_path=database_path
    )

    active_plan = (
        second_store.get_active_plan()
    )

    assert active_plan is not None
    assert active_plan.id == plan_id
    assert (
        active_plan.status
        is PlanStatus.ACTIVE
    )


def test_task_status_survives_store_restart(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    first_store = PlanStore(
        database_path=database_path
    )

    plan_id = first_store.create_plan(
        _make_plan()
    )

    tasks = first_store.get_tasks(
        plan_id
    )

    task_id = tasks[0].id

    first_store.update_task_status(
        task_id,
        TaskStatus.IN_PROGRESS,
    )

    second_store = PlanStore(
        database_path=database_path
    )

    reloaded_tasks = (
        second_store.get_tasks(
            plan_id
        )
    )

    reloaded = next(
        task
        for task in reloaded_tasks
        if task.id == task_id
    )

    assert (
        reloaded.status
        is TaskStatus.IN_PROGRESS
    )


def test_task_result_is_persisted(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        _make_plan()
    )

    task = store.get_tasks(
        plan_id
    )[0]

    store.set_task_result(
        task.id,
        result_summary=(
            "Подключение к SQLite создано."
        ),
        result_artifacts=[
            "app/db/database.py",
            "tests/test_database.py",
        ],
        verification_status="PASS",
        verification_evidence=[
            "6 tests passed",
        ],
    )

    reloaded = store.get_tasks(
        plan_id
    )[0]

    assert reloaded.result_summary == (
        "Подключение к SQLite создано."
    )

    assert reloaded.result_artifacts == [
        "app/db/database.py",
        "tests/test_database.py",
    ]

    assert (
        reloaded.verification_status
        == "PASS"
    )

    assert (
        reloaded.verification_evidence
        == [
            "6 tests passed",
        ]
    )


def test_plan_store_does_not_delete_existing_memories(
    tmp_path,
):
    database_path = (
        tmp_path
        / "shared.db"
    )

    connection = sqlite3.connect(
        database_path
    )

    connection.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL
        )
        """
    )

    connection.execute(
        """
        INSERT INTO memories (
            id,
            content
        )
        VALUES (?, ?)
        """,
        (
            1,
            "Не использовать numpy.",
        ),
    )

    connection.commit()
    connection.close()

    store = PlanStore(
        database_path=database_path
    )

    store.create_plan(
        _make_plan()
    )

    connection = sqlite3.connect(
        database_path
    )

    row = connection.execute(
        """
        SELECT content
        FROM memories
        WHERE id = 1
        """
    ).fetchone()

    connection.close()

    assert row is not None
    assert row[0] == (
        "Не использовать numpy."
    )

def test_task_dependencies_survive_database_round_trip(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan = PlanDraft(
        user_request="Создать backend.",
        global_goal="Создать backend.",
        tasks=[
            TaskDraft(
                key="database",
                title="Настроить БД",
                description="Создать database layer.",
                produces=[
                    "database connection",
                ],
            ),
            TaskDraft(
                key="models",
                title="Создать модели",
                description="Создать модели.",
                requires=[
                    "database connection",
                ],
                depends_on=[
                    "database",
                ],
            ),
        ],
    )

    plan_id = store.create_plan(
        plan
    )

    tasks = store.get_tasks(
        plan_id
    )

    database_task = next(
        task
        for task in tasks
        if task.key == "database"
    )

    models_task = next(
        task
        for task in tasks
        if task.key == "models"
    )

    dependencies = (
        store.get_task_dependencies(
            models_task.id
        )
    )

    assert dependencies == [
        database_task.id
    ]

def test_invalid_dependency_graph_is_not_saved(
    tmp_path,
):
    database_path = (
        tmp_path
        / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    invalid_plan = PlanDraft(
        user_request="Создать backend.",
        global_goal="Создать backend.",
        tasks=[
            TaskDraft(
                key="models",
                title="Создать модели",
                description="Создать модели.",
                depends_on=[
                    "database",
                ],
            ),
        ],
    )

    import pytest
    from app.tasks.dependencies import (
        DependencyValidationError,
    )

    with pytest.raises(
        DependencyValidationError
    ):
        store.create_plan(
            invalid_plan
        )

    assert store.get_active_plan() is None


def test_old_tasks_table_is_migrated_for_dependencies(
    tmp_path,
):
    database_path = (
        tmp_path
        / "old_planner.db"
    )

    connection = sqlite3.connect(
        database_path
    )

    connection.execute(
        """
        CREATE TABLE plans (
            id INTEGER
                PRIMARY KEY AUTOINCREMENT,

            version INTEGER
                NOT NULL
                DEFAULT 1,

            user_request TEXT
                NOT NULL,

            global_goal TEXT
                NOT NULL,

            status TEXT
                NOT NULL
                DEFAULT 'ACTIVE',

            created_at DATETIME
                NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            updated_at DATETIME
                NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE tasks (
            id INTEGER
                PRIMARY KEY AUTOINCREMENT,

            plan_id INTEGER
                NOT NULL,

            parent_id INTEGER,

            title TEXT
                NOT NULL,

            description TEXT
                NOT NULL,

            status TEXT
                NOT NULL
                DEFAULT 'PENDING',

            priority INTEGER
                NOT NULL
                DEFAULT 50,

            requires_json TEXT
                NOT NULL
                DEFAULT '[]',

            produces_json TEXT
                NOT NULL
                DEFAULT '[]',

            success_criteria_json TEXT
                NOT NULL
                DEFAULT '[]',

            current_step INTEGER,

            attempt_count INTEGER
                NOT NULL
                DEFAULT 0,

            result_summary TEXT,

            result_artifacts_json TEXT
                NOT NULL
                DEFAULT '[]',

            verification_status TEXT,

            verification_evidence_json TEXT
                NOT NULL
                DEFAULT '[]',

            created_at DATETIME
                NOT NULL
                DEFAULT CURRENT_TIMESTAMP,

            started_at DATETIME,

            finished_at DATETIME,

            updated_at DATETIME
                NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.commit()
    connection.close()

    PlanStore(
        database_path=database_path
    )

    connection = sqlite3.connect(
        database_path
    )

    task_columns = {
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(tasks)"
        ).fetchall()
    }

    dependency_table = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE
            type = 'table'
            AND name = 'task_dependencies'
        """
    ).fetchone()

    connection.close()

    assert "task_key" in task_columns
    assert dependency_table is not None

def test_external_dependencies_survive_database_round_trip(
    tmp_path,
):
    database_path = (
        tmp_path / "planner.db"
    )

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        PlanDraft(
            user_request="Создать API.",
            global_goal="Создать API.",
            tasks=[
                TaskDraft(
                    key="api",
                    title="Создать API",
                    description="Создать API.",
                    external_dependencies=[
                        "flask",
                        "pytest",
                    ],
                )
            ],
        )
    )

    task = store.get_tasks(
        plan_id
    )[0]

    assert task.external_dependencies == [
        "flask",
        "pytest",
    ]