from __future__ import annotations

from app.agent.planner.decomposer import (
    PlannerError,
    normalize_resource,
)
from app.tasks.models import TaskDraft


class ContractBuilder:
    """
    Детерминированная проверка Task contracts.

    requires ресурс обязан производиться ровно
    одной другой Task. Это защищает Task OS
    от неоднозначных планов модели.
    """

    def build(
        self,
        task: TaskDraft,
    ) -> dict[str, list[str]]:
        return {
            "success_criteria": list(
                task.success_criteria
            ),
            "requires": list(task.requires),
            "produces": list(task.produces),
            "external_dependencies": list(
                task.external_dependencies
            ),
        }

    def validate_contracts(
        self,
        tasks: list[TaskDraft],
    ) -> None:
        task_keys: set[str] = set()

        producers: dict[
            str,
            list[str],
        ] = {}

        # --------------------------------------
        # TASK KEYS + PRODUCERS
        # --------------------------------------

        for task in tasks:
            if (
                task.key is None
                or not task.key.strip()
            ):
                raise PlannerError(
                    "Task contract invalid: "
                    "task key is required"
                )

            normalized_key = (
                task.key.strip().casefold()
            )

            if normalized_key in task_keys:
                raise PlannerError(
                    "Task contract invalid: "
                    "duplicate task key "
                    f"'{task.key}'"
                )

            task_keys.add(normalized_key)

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

        # --------------------------------------
        # REQUIRES -> PRODUCES
        # --------------------------------------

        for task in tasks:
            if task.key is None:
                raise PlannerError(
                    "Task contract invalid: "
                    "task key is required"
                )

            for requirement in task.requires:
                normalized_requirement = (
                    normalize_resource(
                        requirement
                    )
                )

                producer_keys = [
                    producer_key
                    for producer_key in producers.get(
                        normalized_requirement,
                        [],
                    )
                    if (
                        producer_key.casefold()
                        != task.key.casefold()
                    )
                ]

                if not producer_keys:
                    raise PlannerError(
                        "Task contract invalid: "
                        f"task '{task.key}' "
                        "requires resource "
                        f"'{requirement}', "
                        "but it has no producer"
                    )

                if len(producer_keys) > 1:
                    raise PlannerError(
                        "Task contract invalid: "
                        f"task '{task.key}' "
                        "requires resource "
                        f"'{requirement}', "
                        "but it has multiple "
                        "producers: "
                        + ", ".join(
                            producer_keys
                        )
                    )

