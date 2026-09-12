from __future__ import annotations

import json
from copy import deepcopy

from app.agent.planner.decomposer import (
    PlannerError,
    generate_with_repair,
    normalize_resource,
    parse_json_object,
)
from app.tasks.dependencies import validate_task_graph
from app.tasks.models import TaskDraft


class DependencyBuilder:
    """
    Третий семантический проход Planner.

    LLM предлагает DAG, но Python проверяет граф и
    при необходимости строит зависимости детерминированно
    из requires/produces.
    """

    def __init__(
        self,
        llm=None,
        *,
        max_repair_attempts: int = 0,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError(
                "max_repair_attempts "
                "cannot be negative"
            )

        self.llm = llm
        self.max_repair_attempts = (
            max_repair_attempts
        )

    def build(
        self,
        *,
        user_request: str,
        goal: dict[str, object],
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        if self.llm is None:
            return self.infer_from_contracts(
                tasks
            )

        task_payload = [
            {
                "key": task.key,
                "title": task.title,
                "requires": task.requires,
                "external_dependencies": (
                    task.external_dependencies
                ),
                "produces": task.produces,
            }
            for task in tasks
        ]

        dependency_repair_context = json.dumps(
            {
                "allowed_task_keys": [
                    task.key
                    for task in tasks
                ],
                "tasks": task_payload,
            },
            ensure_ascii=False,
            indent=2,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Dependency "
                    "Builder inside a coding "
                    "planner. Return JSON only. "
                    "Build a DAG. "
                    "A task may depend only on "
                    "task keys that exist. "
                    "Do not create cycles. "
                    "external_dependencies NEVER create "
                    "task dependencies. Only requires "
                    "and produces determine dependencies."
                ),
            },
            {
                "role": "user",
                "content": (
                    "USER REQUEST:\n"
                    f"{user_request}\n\n"
                    "GOAL:\n"
                    + json.dumps(
                        goal,
                        ensure_ascii=False,
                    )
                    + "\n\nTASKS:\n"
                    + json.dumps(
                        task_payload,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n\n"
                    "Return dependencies for "
                    "EVERY task key:\n"
                    "{\n"
                    '  "dependencies": {\n'
                    '    "task_key": '
                    '["dependency_key"]\n'
                    "  }\n"
                    "}"
                ),
            },
        ]

        try:
            return generate_with_repair(
                self.llm,
                initial_messages=messages,
                parser=lambda response: (
                    self.parse_response(
                        response=response,
                        tasks=tasks,
                    )
                ),
                max_new_tokens=640,
                stage_name="DEPENDENCY_BUILDER",
                max_repair_attempts=(
                    self.max_repair_attempts
                ),
                repair_context=(
                    dependency_repair_context
                ),
            )

        except PlannerError as model_error:
            # В strict mode модель не исправляется
            # автоматически.
            if self.max_repair_attempts == 0:
                raise PlannerError(
                    "Invalid task graph: "
                    f"{model_error}"
                ) from model_error

            # В runtime включается детерминированный
            # fallback из requires/produces.
            try:
                return self.infer_from_contracts(
                    tasks
                )

            except Exception as fallback_error:
                raise PlannerError(
                    "Invalid task graph. "
                    "Model dependency builder "
                    "failed: "
                    f"{model_error}. "
                    "Deterministic dependency "
                    "fallback also failed: "
                    f"{fallback_error}"
                ) from fallback_error

    def parse_response(
        self,
        *,
        response: str,
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        data = parse_json_object(response)

        dependencies = data.get("dependencies")

        if not isinstance(dependencies, dict):
            raise PlannerError(
                "dependencies must be "
                "a JSON object"
            )

        expected_keys = {
            str(task.key)
            for task in tasks
        }

        returned_keys = {
            str(key)
            for key in dependencies
        }

        if returned_keys != expected_keys:
            raise PlannerError(
                "dependencies must contain "
                "every task exactly once"
            )

        result = deepcopy(tasks)

        for task in result:
            if task.key is None:
                raise PlannerError(
                    "task key is required"
                )

            raw_dependencies = dependencies[
                task.key
            ]

            if not isinstance(
                raw_dependencies,
                list,
            ):
                raise PlannerError(
                    "dependencies for "
                    f"'{task.key}' "
                    "must be a list"
                )

            parsed: list[str] = []

            for dependency in raw_dependencies:
                if (
                    not isinstance(dependency, str)
                    or not dependency.strip()
                ):
                    raise PlannerError(
                        "dependency keys "
                        "must be strings"
                    )

                parsed.append(
                    dependency.strip()
                )

            task.depends_on = parsed

        validate_task_graph(result)

        return result

    def infer_from_contracts(
        self,
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        """
        Детерминированный fallback.

        Строит зависимости только из:
        Task.requires <-> Task.produces.
        """

        result = deepcopy(tasks)

        producers: dict[
            str,
            list[str],
        ] = {}

        for task in result:
            if task.key is None:
                raise PlannerError(
                    "task key is required "
                    "for dependency inference"
                )

            for product in task.produces:
                normalized_product = (
                    normalize_resource(product)
                )

                if not normalized_product:
                    continue

                producers.setdefault(
                    normalized_product,
                    [],
                ).append(task.key)

        for task in result:
            if task.key is None:
                raise PlannerError(
                    "task key is required "
                    "for dependency inference"
                )

            dependencies: list[str] = []

            for requirement in task.requires:
                normalized_requirement = (
                    normalize_resource(
                        requirement
                    )
                )

                matching_producers = [
                    producer_key
                    for producer_key
                    in producers.get(
                        normalized_requirement,
                        [],
                    )
                    if producer_key != task.key
                ]

                if not matching_producers:
                    raise PlannerError(
                        "Cannot infer dependency "
                        f"for task '{task.key}': "
                        "required resource "
                        f"'{requirement}' "
                        "has no producer"
                    )

                if len(matching_producers) > 1:
                    raise PlannerError(
                        "Cannot infer dependency "
                        f"for task '{task.key}': "
                        "required resource "
                        f"'{requirement}' "
                        "has multiple producers: "
                        + ", ".join(
                            matching_producers
                        )
                    )

                dependency_key = (
                    matching_producers[0]
                )

                if (
                    dependency_key
                    not in dependencies
                ):
                    dependencies.append(
                        dependency_key
                    )

            task.depends_on = dependencies

        validate_task_graph(result)

        return result


