from __future__ import annotations

import pytest

from app.tasks.dependencies import (
    DependencyValidationError,
    validate_task_graph,
)
from app.tasks.models import TaskDraft


def test_valid_dependency_graph_passes():
    tasks = [
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
            description="Создать модели данных.",
            requires=[
                "database connection",
            ],
            produces=[
                "User model",
                "Note model",
            ],
            depends_on=[
                "database",
            ],
        ),
        TaskDraft(
            key="migrations",
            title="Создать миграции",
            description="Создать migrations.",
            requires=[
                "database connection",
                "User model",
                "Note model",
            ],
            depends_on=[
                "database",
                "models",
            ],
        ),
    ]

    validate_task_graph(tasks)


def test_duplicate_task_key_is_rejected():
    tasks = [
        TaskDraft(
            key="database",
            title="Первая",
            description="Первая.",
        ),
        TaskDraft(
            key="database",
            title="Вторая",
            description="Вторая.",
        ),
    ]

    with pytest.raises(
        DependencyValidationError,
        match="duplicate",
    ):
        validate_task_graph(tasks)


def test_dependency_on_unknown_task_is_rejected():
    tasks = [
        TaskDraft(
            key="models",
            title="Создать модели",
            description="Создать модели.",
            depends_on=[
                "database",
            ],
        )
    ]

    with pytest.raises(
        DependencyValidationError,
        match="unknown",
    ):
        validate_task_graph(tasks)


def test_task_cannot_depend_on_itself():
    tasks = [
        TaskDraft(
            key="database",
            title="Настроить БД",
            description="Настроить БД.",
            depends_on=[
                "database",
            ],
        )
    ]

    with pytest.raises(
        DependencyValidationError,
        match="itself",
    ):
        validate_task_graph(tasks)


def test_cycle_is_rejected():
    tasks = [
        TaskDraft(
            key="a",
            title="A",
            description="A.",
            depends_on=["c"],
        ),
        TaskDraft(
            key="b",
            title="B",
            description="B.",
            depends_on=["a"],
        ),
        TaskDraft(
            key="c",
            title="C",
            description="C.",
            depends_on=["b"],
        ),
    ]

    with pytest.raises(
        DependencyValidationError,
        match="cycle",
    ):
        validate_task_graph(tasks)


def test_missing_required_product_is_rejected():
    tasks = [
        TaskDraft(
            key="api",
            title="Создать API",
            description="Создать API.",
            requires=[
                "authentication service",
            ],
        )
    ]

    with pytest.raises(
        DependencyValidationError,
        match="authentication service",
    ):
        validate_task_graph(tasks)


def test_required_product_must_be_in_dependency_chain():
    tasks = [
        TaskDraft(
            key="auth",
            title="Создать auth",
            description="Создать auth.",
            produces=[
                "authentication service",
            ],
        ),
        TaskDraft(
            key="api",
            title="Создать API",
            description="Создать API.",
            requires=[
                "authentication service",
            ],
            depends_on=[],
        ),
    ]

    with pytest.raises(
        DependencyValidationError,
        match="dependency",
    ):
        validate_task_graph(tasks)


def test_transitive_dependency_can_satisfy_requirement():
    tasks = [
        TaskDraft(
            key="database",
            title="БД",
            description="БД.",
            produces=[
                "database connection",
            ],
        ),
        TaskDraft(
            key="models",
            title="Модели",
            description="Модели.",
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
            title="API",
            description="API.",
            requires=[
                "database connection",
                "User model",
            ],
            depends_on=[
                "models",
            ],
        ),
    ]

    validate_task_graph(tasks)