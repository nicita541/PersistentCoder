from __future__ import annotations

from app.agent.planner.decomposer import (
    PlannerError,
    optional_string_list,
    require_string,
    require_string_list,
)
from app.tasks.models import StepDraft, TaskDraft


class TaskBuilder:
    """
    Python-сторона планирования.

    LLM принимает semantic decisions (components),
    а TaskBuilder детерминированно строит настоящие
    TaskDraft: ключи, priority, contracts, criteria.
    """

    MAX_TASKS = 10

    def __init__(
        self,
        *,
        max_tasks: int = MAX_TASKS,
    ) -> None:
        if max_tasks < 1:
            raise ValueError(
                "max_tasks must be at least 1"
            )

        self.max_tasks = max_tasks

    def build(
        self,
        components: list[dict[str, object]],
    ) -> list[TaskDraft]:
        count = len(components)

        if not (1 <= count <= self.max_tasks):
            raise PlannerError(
                f"task count must be "
                f"1..{self.max_tasks}"
            )

        result: list[TaskDraft] = []

        for index, raw_task in enumerate(
            components,
            start=1,
        ):
            if not isinstance(raw_task, dict):
                raise PlannerError(
                    "each task must be "
                    "a JSON object"
                )

            key = require_string(
                raw_task,
                "key",
            )

            title = require_string(
                raw_task,
                "title",
            )

            description = require_string(
                raw_task,
                "description",
            )

            priority = raw_task.get(
                "priority",
                50,
            )

            if (
                not isinstance(priority, int)
                or isinstance(priority, bool)
            ):
                raise PlannerError(
                    f"task {index} priority "
                    "must be integer"
                )

            priority = max(
                0,
                min(100, priority),
            )

            requires = require_string_list(
                raw_task,
                "requires",
            )

            produces = require_string_list(
                raw_task,
                "produces",
            )

            success_criteria = (
                require_string_list(
                    raw_task,
                    "success_criteria",
                )
            )

            if not success_criteria:
                raise PlannerError(
                    f"task '{key}' requires "
                    "success_criteria"
                )

            external_dependencies = (
                optional_string_list(
                    raw_task,
                    "external_dependencies",
                )
            )

            result.append(
                TaskDraft(
                    key=key,
                    title=title,
                    description=description,
                    priority=priority,
                    requires=requires,
                    produces=produces,
                    success_criteria=(
                        success_criteria
                    ),
                    external_dependencies=(
                        external_dependencies
                    ),
                )
            )

        return result

    def build_steps(
        self,
        components: list[dict[str, object]],
    ) -> dict[str, list[StepDraft]]:
        """
        Build per-task Steps from the semantic components.

        A component may declare its own ordered `steps`. Simple tasks
        may omit them (a single default Step is then used).
        """

        steps_by_key: dict[str, list[StepDraft]] = {}

        for raw_task in components:
            if not isinstance(raw_task, dict):
                raise PlannerError(
                    "each task must be a JSON object"
                )

            key = require_string(raw_task, "key")

            raw_steps = raw_task.get("steps", [])

            if raw_steps is None:
                raw_steps = []

            if not isinstance(raw_steps, list):
                raise PlannerError(
                    f"steps for '{key}' must be a list"
                )

            steps: list[StepDraft] = []

            for raw_step in raw_steps:
                if not isinstance(raw_step, dict):
                    raise PlannerError(
                        "each step must be a JSON object"
                    )

                title = require_string(
                    raw_step,
                    "title",
                )

                description = require_string(
                    raw_step,
                    "description",
                )

                criteria = optional_string_list(
                    raw_step,
                    "success_criteria",
                )

                if not criteria:
                    criteria = [title]

                steps.append(
                    StepDraft(
                        title=title,
                        description=description,
                        requires=optional_string_list(
                            raw_step,
                            "requires",
                        ),
                        produces=optional_string_list(
                            raw_step,
                            "produces",
                        ),
                        success_criteria=criteria,
                    )
                )

            steps_by_key[key] = steps

        return steps_by_key


