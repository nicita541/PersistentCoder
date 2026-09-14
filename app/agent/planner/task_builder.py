from __future__ import annotations

from dataclasses import replace

from app.agent.planner.decomposer import (
    PlannerError,
    optional_string_list,
    require_string,
    require_string_list,
)
from app.tasks.models import StepDraft, TaskDraft
from app.tasks.verification_spec import parse_verification_specs
from app.tasks.change_scope import canonicalize_change_paths


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

            change_paths = optional_string_list(
                raw_task,
                "change_paths",
            )

            try:
                verification_specs = parse_verification_specs(
                    raw_task.get("verification_specs", [])
                )
            except ValueError as error:
                raise PlannerError(
                    f"task '{key}' has invalid verification_specs: {error}"
                ) from error

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
                    change_paths=change_paths,
                    verification_specs=verification_specs,
                )
            )

        return result

    @staticmethod
    def key_require_edges(
        tasks: list[TaskDraft],
    ) -> dict[str, list[str]]:
        """
        Which tasks named a SIBLING TASK KEY inside `requires`.

        The edges are remembered so they can be restored after the
        dependency stage, which owns the final `depends_on` list.
        """

        keys = {
            str(task.key).strip().casefold(): str(
                task.key
            )
            for task in tasks
            if task.key
        }

        edges: dict[str, list[str]] = {}

        for task in tasks:
            own = str(task.key or "").strip().casefold()

            moved: list[str] = []

            for requirement in task.requires:
                lookup = str(requirement).strip().casefold()

                if lookup in keys and lookup != own:
                    moved.append(keys[lookup])

            if moved and task.key:
                edges[str(task.key)] = moved

        return edges

    @staticmethod
    def merge_key_require_edges(
        tasks: list[TaskDraft],
        edges: dict[str, list[str]],
    ) -> list[TaskDraft]:
        """
        Restore remembered key->key edges into the final depends_on
        lists (only for task keys that still exist).
        """

        if not edges:
            return tasks

        known = {
            str(task.key)
            for task in tasks
            if task.key
        }

        merged: list[TaskDraft] = []

        for task in tasks:
            extra = [
                key
                for key in edges.get(str(task.key), [])
                if key in known
                and key != str(task.key)
            ]

            if not extra:
                merged.append(task)
                continue

            depends_on = list(task.depends_on)

            for key in extra:
                if key not in depends_on:
                    depends_on.append(key)

            merged.append(
                replace(task, depends_on=depends_on)
            )

        return merged

    @staticmethod
    def normalize_key_requires(
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        """
        Deterministic interpretation of one very common small-model
        mistake: putting a SIBLING TASK KEY into `requires` instead of
        a produced resource.

        Such an entry is moved to `depends_on` (still validated as a
        DAG). The requires/produces contract itself is untouched: only
        entries that are exactly a sibling task key are affected.
        """

        keys = {
            str(task.key).strip().casefold(): str(
                task.key
            )
            for task in tasks
            if task.key
        }

        normalized: list[TaskDraft] = []

        for task in tasks:
            own = str(task.key or "").strip().casefold()

            moved: list[str] = []
            remaining: list[str] = []

            for requirement in task.requires:
                lookup = str(requirement).strip().casefold()

                if lookup in keys and lookup != own:
                    moved.append(lookup)
                    continue

                remaining.append(requirement)

            if not moved:
                normalized.append(task)
                continue

            depends_on = list(task.depends_on)

            for lookup in moved:
                key = keys[lookup]

                if key not in depends_on:
                    depends_on.append(key)

            normalized.append(
                replace(
                    task,
                    requires=remaining,
                    depends_on=depends_on,
                )
            )

        return normalized

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

            task_paths = canonicalize_change_paths(
                optional_string_list(raw_task, "change_paths")
            )
            if task_paths:
                try:
                    task_specs = parse_verification_specs(
                        raw_task.get("verification_specs", [])
                    )
                except ValueError as error:
                    raise PlannerError(
                        f"task '{key}' has invalid verification specs: {error}"
                    ) from error

                file_steps: list[StepDraft] = []
                for path in task_paths:
                    path_specs = [
                        spec for spec in task_specs if spec.target == path
                    ]
                    if not path_specs:
                        raise PlannerError(
                            f"owned file '{path}' requires a structured verification spec"
                        )
                    file_steps.append(
                        StepDraft(
                            title=f"Implement {path}",
                            description=(
                                f"Change only {path}. "
                                + require_string(raw_task, "description")
                            ),
                            requires=optional_string_list(raw_task, "requires"),
                            produces=[path],
                            success_criteria=optional_string_list(
                                raw_task, "success_criteria"
                            ),
                            change_paths=[path],
                            verification_specs=path_specs,
                        )
                    )
                steps_by_key[key] = file_steps
                continue

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
                        change_paths=optional_string_list(
                            raw_step,
                            "change_paths",
                        ),
                        verification_specs=parse_verification_specs(
                            raw_step.get("verification_specs", [])
                        ),
                    )
                )

            steps_by_key[key] = steps

        return steps_by_key


