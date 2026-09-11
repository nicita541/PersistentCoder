from __future__ import annotations

from collections import defaultdict

from app.tasks.models import TaskDraft


class DependencyValidationError(
    ValueError
):
    pass


def _normalize_key(
    value: str,
) -> str:
    return value.strip().casefold()


def _normalize_resource(
    value: str,
) -> str:
    return value.strip().casefold()


def _build_task_map(
    tasks: list[TaskDraft],
) -> dict[str, TaskDraft]:
    task_map: dict[
        str,
        TaskDraft,
    ] = {}

    for task in tasks:
        if (
            task.key is None
            or not task.key.strip()
        ):
            raise DependencyValidationError(
                "task key is required"
            )

        key = _normalize_key(
            task.key
        )

        if key in task_map:
            raise DependencyValidationError(
                f"duplicate task key: {task.key}"
            )

        task_map[key] = task

    return task_map


def _validate_dependency_references(
    task_map: dict[str, TaskDraft],
) -> None:
    for task_key, task in task_map.items():
        seen_dependencies: set[str] = set()

        for raw_dependency in task.depends_on:
            dependency = _normalize_key(
                raw_dependency
            )

            if dependency == task_key:
                raise DependencyValidationError(
                    f"task {task.key} "
                    "cannot depend on itself"
                )

            if dependency not in task_map:
                raise DependencyValidationError(
                    f"unknown dependency: "
                    f"{raw_dependency}"
                )

            if dependency in seen_dependencies:
                continue

            seen_dependencies.add(
                dependency
            )


def _validate_no_cycles(
    task_map: dict[str, TaskDraft],
) -> None:
    """
    DFS state:

    0 = unvisited
    1 = visiting
    2 = finished
    """

    state: dict[str, int] = {
        key: 0
        for key in task_map
    }

    def visit(
        task_key: str,
    ) -> None:
        if state[task_key] == 1:
            raise DependencyValidationError(
                "dependency cycle detected"
            )

        if state[task_key] == 2:
            return

        state[task_key] = 1

        task = task_map[
            task_key
        ]

        for raw_dependency in task.depends_on:
            dependency = _normalize_key(
                raw_dependency
            )

            visit(
                dependency
            )

        state[task_key] = 2

    for key in task_map:
        if state[key] == 0:
            visit(
                key
            )


def _get_dependency_closure(
    *,
    task_key: str,
    task_map: dict[str, TaskDraft],
) -> set[str]:
    """
    Возвращает все прямые и транзитивные
    зависимости Task.

    Например:

    api -> models -> database

    closure(api) =
    {"models", "database"}
    """

    result: set[str] = set()
    stack: list[str] = []

    task = task_map[
        task_key
    ]

    for dependency in task.depends_on:
        stack.append(
            _normalize_key(
                dependency
            )
        )

    while stack:
        current = stack.pop()

        if current in result:
            continue

        result.add(
            current
        )

        dependency_task = (
            task_map[current]
        )

        for parent in (
            dependency_task.depends_on
        ):
            stack.append(
                _normalize_key(
                    parent
                )
            )

    return result


def _validate_requires_and_produces(
    task_map: dict[str, TaskDraft],
) -> None:
    producers: dict[
        str,
        set[str],
    ] = defaultdict(set)

    # -----------------------------------------
    # Кто PRODUCES каждый ресурс?
    # -----------------------------------------

    for task_key, task in task_map.items():
        for product in task.produces:
            normalized_product = (
                _normalize_resource(
                    product
                )
            )

            if not normalized_product:
                continue

            producers[
                normalized_product
            ].add(
                task_key
            )

    # -----------------------------------------
    # Каждый REQUIRES должен иметь producer,
    # который находится в dependency chain.
    # -----------------------------------------

    for task_key, task in task_map.items():
        dependency_closure = (
            _get_dependency_closure(
                task_key=task_key,
                task_map=task_map,
            )
        )

        for requirement in task.requires:
            normalized_requirement = (
                _normalize_resource(
                    requirement
                )
            )

            producer_keys = producers.get(
                normalized_requirement,
                set(),
            )

            if not producer_keys:
                raise DependencyValidationError(
                    "required product "
                    f"'{requirement}' "
                    "has no producer"
                )

            valid_producers = (
                producer_keys
                & dependency_closure
            )

            if not valid_producers:
                raise DependencyValidationError(
                    "required product "
                    f"'{requirement}' exists, "
                    "but its producer is not "
                    "in the dependency chain"
                )


def validate_task_graph(
    tasks: list[TaskDraft],
) -> None:
    """
    Проверяет Planner DAG до записи в SQLite.

    Проверки:

    1. stable key существует;
    2. key уникальны;
    3. depends_on указывает на существующие Task;
    4. Task не зависит от самой себя;
    5. граф не содержит циклов;
    6. каждый requires имеет producer;
    7. producer находится в dependency chain.
    """

    task_map = _build_task_map(
        tasks
    )

    _validate_dependency_references(
        task_map
    )

    _validate_no_cycles(
        task_map
    )

    _validate_requires_and_produces(
        task_map
    )