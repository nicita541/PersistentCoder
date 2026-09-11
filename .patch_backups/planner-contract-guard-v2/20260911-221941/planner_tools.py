from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from app.tasks.dependencies import (
    DependencyValidationError,
    validate_task_graph,
)
from app.tasks.models import PlanDraft, TaskDraft


class PlannerToolError(RuntimeError):
    pass


class PlannerWorkspace:
    KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

    def __init__(
        self,
        *,
        user_request: str,
        global_goal: str | None = None,
    ) -> None:
        request = user_request.strip()
        if not request:
            raise PlannerToolError(
                "user request is required"
            )

        self.user_request = request
        self.global_goal = (
            global_goal.strip()
            if global_goal
            else request
        )

        self._tasks: dict[str, TaskDraft] = {}

    @staticmethod
    def _clean_string(
        value: Any,
        *,
        field_name: str,
    ) -> str:
        if not isinstance(value, str):
            raise PlannerToolError(
                f"{field_name} must be a string"
            )

        value = value.strip()

        if not value:
            raise PlannerToolError(
                f"{field_name} cannot be empty"
            )

        return value

    @classmethod
    def _clean_string_list(
        cls,
        value: Any,
        *,
        field_name: str,
    ) -> list[str]:
        if not isinstance(value, list):
            raise PlannerToolError(
                f"{field_name} must be a list"
            )

        result: list[str] = []

        for item in value:
            cleaned = cls._clean_string(
                item,
                field_name=field_name,
            )

            if cleaned not in result:
                result.append(cleaned)

        return result

    @staticmethod
    def _normalize_resource(
        value: str,
    ) -> str:
        return " ".join(
            value.casefold().split()
        )

    def create_task(
        self,
        *,
        key: str,
        title: str,
        description: str,
        priority: int = 50,
        success_criteria: list[str],
    ) -> dict[str, Any]:
        key = self._clean_string(
            key,
            field_name="key",
        ).casefold()

        if not self.KEY_PATTERN.fullmatch(
            key
        ):
            raise PlannerToolError(
                "task key must match "
                "^[a-z][a-z0-9_]{0,63}$"
            )

        if key in self._tasks:
            raise PlannerToolError(
                f"task already exists: {key}"
            )

        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
        ):
            raise PlannerToolError(
                "priority must be an integer"
            )

        priority = max(
            0,
            min(100, priority),
        )

        criteria = (
            self._clean_string_list(
                success_criteria,
                field_name="success_criteria",
            )
        )

        if not criteria:
            raise PlannerToolError(
                "success_criteria cannot be empty"
            )

        task = TaskDraft(
            key=key,
            title=self._clean_string(
                title,
                field_name="title",
            ),
            description=self._clean_string(
                description,
                field_name="description",
            ),
            priority=priority,
            requires=[],
            produces=[],
            depends_on=[],
            external_dependencies=[],
            success_criteria=criteria,
        )

        self._tasks[key] = task

        return {
            "ok": True,
            "task_key": key,
        }

    def update_task(
        self,
        *,
        task_key: str,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
        success_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        task = self._get_task(
            task_key
        )

        if title is not None:
            task.title = self._clean_string(
                title,
                field_name="title",
            )

        if description is not None:
            task.description = (
                self._clean_string(
                    description,
                    field_name="description",
                )
            )

        if priority is not None:
            if (
                not isinstance(priority, int)
                or isinstance(priority, bool)
            ):
                raise PlannerToolError(
                    "priority must be an integer"
                )

            task.priority = max(
                0,
                min(100, priority),
            )

        if success_criteria is not None:
            criteria = (
                self._clean_string_list(
                    success_criteria,
                    field_name=(
                        "success_criteria"
                    ),
                )
            )

            if not criteria:
                raise PlannerToolError(
                    "success_criteria "
                    "cannot be empty"
                )

            task.success_criteria = criteria

        return {
            "ok": True,
            "task_key": task.key,
        }

    def set_task_contract(
        self,
        *,
        task_key: str,
        requires: list[str],
        produces: list[str],
        external_dependencies: list[str],
    ) -> dict[str, Any]:
        task = self._get_task(
            task_key
        )

        task.requires = (
            self._clean_string_list(
                requires,
                field_name="requires",
            )
        )

        task.produces = (
            self._clean_string_list(
                produces,
                field_name="produces",
            )
        )

        task.external_dependencies = (
            self._clean_string_list(
                external_dependencies,
                field_name=(
                    "external_dependencies"
                ),
            )
        )

        task.depends_on = []

        return {
            "ok": True,
            "task_key": task.key,
        }

    def remove_task(
        self,
        *,
        task_key: str,
    ) -> dict[str, Any]:
        key = self._clean_string(
            task_key,
            field_name="task_key",
        ).casefold()

        if key not in self._tasks:
            raise PlannerToolError(
                f"unknown task: {key}"
            )

        del self._tasks[key]

        return {
            "ok": True,
            "removed": key,
        }

    def inspect_plan(
        self,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "global_goal": self.global_goal,
            "tasks": [
                self._task_to_dict(task)
                for task
                in self._tasks.values()
            ],
        }

    def validate_plan(
        self,
    ) -> dict[str, Any]:
        try:
            tasks = (
                self._build_validated_tasks()
            )

        except (
            PlannerToolError,
            DependencyValidationError,
        ) as error:
            return {
                "ok": False,
                "errors": [
                    str(error)
                ],
            }

        return {
            "ok": True,
            "task_count": len(tasks),
            "tasks": [
                self._task_to_dict(task)
                for task in tasks
            ],
        }

    def finish_plan(
        self,
    ) -> dict[str, Any]:
        validation = self.validate_plan()

        if not validation["ok"]:
            return validation

        return {
            "ok": True,
            "finished": True,
            "task_count": (
                validation["task_count"]
            ),
        }

    def to_plan_draft(
        self,
    ) -> PlanDraft:
        tasks = self._build_validated_tasks()

        return PlanDraft(
            user_request=self.user_request,
            global_goal=self.global_goal,
            tasks=tasks,
        )

    def _get_task(
        self,
        task_key: str,
    ) -> TaskDraft:
        key = self._clean_string(
            task_key,
            field_name="task_key",
        ).casefold()

        task = self._tasks.get(key)

        if task is None:
            raise PlannerToolError(
                f"unknown task: {key}"
            )

        return task

    def _build_validated_tasks(
        self,
    ) -> list[TaskDraft]:
        if not self._tasks:
            raise PlannerToolError(
                "plan must contain at least one task"
            )

        tasks = deepcopy(
            list(self._tasks.values())
        )

        producers: dict[
            str,
            list[str],
        ] = {}

        for task in tasks:
            if task.key is None:
                raise PlannerToolError(
                    "task key is required"
                )

            for product in task.produces:
                normalized = (
                    self._normalize_resource(
                        product
                    )
                )

                producers.setdefault(
                    normalized,
                    [],
                ).append(
                    task.key
                )

        for task in tasks:
            if task.key is None:
                raise PlannerToolError(
                    "task key is required"
                )

            dependencies: list[str] = []

            for requirement in task.requires:
                normalized = (
                    self._normalize_resource(
                        requirement
                    )
                )

                matches = [
                    producer_key
                    for producer_key
                    in producers.get(
                        normalized,
                        [],
                    )
                    if producer_key
                    != task.key
                ]

                if not matches:
                    raise PlannerToolError(
                        f"task '{task.key}' "
                        f"requires resource "
                        f"'{requirement}', "
                        "but it has no producer"
                    )

                if len(matches) > 1:
                    raise PlannerToolError(
                        f"task '{task.key}' "
                        f"requires resource "
                        f"'{requirement}', "
                        "but it has multiple "
                        "producers: "
                        + ", ".join(matches)
                    )

                producer_key = matches[0]

                if producer_key not in dependencies:
                    dependencies.append(
                        producer_key
                    )

            task.depends_on = dependencies

        validate_task_graph(
            tasks
        )

        return tasks

    @staticmethod
    def _task_to_dict(
        task: TaskDraft,
    ) -> dict[str, Any]:
        return {
            "key": task.key,
            "title": task.title,
            "description": task.description,
            "priority": task.priority,
            "requires": list(
                task.requires
            ),
            "produces": list(
                task.produces
            ),
            "external_dependencies": list(
                task.external_dependencies
            ),
            "depends_on": list(
                task.depends_on
            ),
            "success_criteria": list(
                task.success_criteria
            ),
        }


class PlannerToolRouter:
    TOOL_SCHEMAS: dict[
        str,
        tuple[set[str], set[str]],
    ] = {
        "create_task": (
            {
                "key",
                "title",
                "description",
                "success_criteria",
            },
            {
                "priority",
            },
        ),
        "update_task": (
            {
                "task_key",
            },
            {
                "title",
                "description",
                "priority",
                "success_criteria",
            },
        ),
        "set_task_contract": (
            {
                "task_key",
                "requires",
                "produces",
                "external_dependencies",
            },
            set(),
        ),
        "remove_task": (
            {
                "task_key",
            },
            set(),
        ),
        "inspect_plan": (
            set(),
            set(),
        ),
        "validate_plan": (
            set(),
            set(),
        ),
        "finish_plan": (
            set(),
            set(),
        ),
    }

    def __init__(
        self,
        workspace: PlannerWorkspace,
    ) -> None:
        self.workspace = workspace

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in self.TOOL_SCHEMAS:
            return {
                "ok": False,
                "error": (
                    f"unknown tool: {tool_name}"
                ),
            }

        if not isinstance(arguments, dict):
            return {
                "ok": False,
                "error": (
                    "arguments must be "
                    "a JSON object"
                ),
            }

        required, optional = (
            self.TOOL_SCHEMAS[
                tool_name
            ]
        )

        keys = set(arguments)

        missing = required - keys
        unexpected = keys - (
            required | optional
        )

        if missing:
            return {
                "ok": False,
                "error": (
                    "missing arguments: "
                    + ", ".join(
                        sorted(missing)
                    )
                ),
            }

        if unexpected:
            return {
                "ok": False,
                "error": (
                    "unexpected arguments: "
                    + ", ".join(
                        sorted(unexpected)
                    )
                ),
            }

        try:
            method = getattr(
                self.workspace,
                tool_name,
            )

            return method(
                **arguments
            )

        except (
            PlannerToolError,
            TypeError,
            ValueError,
        ) as error:
            return {
                "ok": False,
                "error": str(error),
            }
