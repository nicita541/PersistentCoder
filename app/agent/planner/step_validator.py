from __future__ import annotations

import re
from pathlib import Path

from app.agent.verifier.criterion import (
    classify_criterion,
)
from app.tasks.models import StepDraft, TaskDraft
from app.tasks.change_scope import AllowedChangeSet, ChangeScopeError


_FILE_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_\-./\\]+"
    r"\.(?:py|pyi|txt|md|json|toml|cfg|ini|ya?ml|js|ts|sql|sh)"
)


def _normalize(
    text: object,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(text or ""),
    ).strip().casefold()


def _file_paths(
    values: list[str],
) -> set[str]:
    found: set[str] = set()

    for value in values or []:
        for raw in _FILE_TOKEN_RE.findall(
            str(value)
        ):
            found.add(
                raw.replace("\\", "/").lstrip("./")
            )

    return found


class StepValidator:
    """
    Deterministic validator for Task -> Step decomposition.

    A Step may not exceed the Task scope, dependencies must be
    consistent, criteria must be verifiable by a real check, and one
    file path has exactly one owning Step.
    """

    MAX_STEPS_PER_TASK = 8

    def __init__(
        self,
        *,
        max_steps_per_task: int = MAX_STEPS_PER_TASK,
    ) -> None:
        if max_steps_per_task < 1:
            raise ValueError(
                "max_steps_per_task must be >= 1"
            )

        self.max_steps_per_task = max_steps_per_task

    def validate(
        self,
        *,
        task: TaskDraft,
        steps: list[StepDraft],
    ) -> list[str]:
        errors: list[str] = []

        key = task.key

        if len(steps) > self.max_steps_per_task:
            errors.append(
                f"task '{key}' has {len(steps)} steps "
                f"(max {self.max_steps_per_task})"
            )

        titles: dict[str, int] = {}
        owners: dict[str, str] = {}

        task_requires = {
            _normalize(item)
            for item in task.requires
        }

        produced_so_far: set[str] = set()

        for index, step in enumerate(steps, start=1):
            title = _normalize(step.title)
            description = _normalize(step.description)

            if not title:
                errors.append(
                    f"task '{key}' step {index} has no title"
                )

            if not description:
                errors.append(
                    f"task '{key}' step {index} "
                    "has no description"
                )

            if title in titles:
                errors.append(
                    f"task '{key}' has duplicate step "
                    f"titles: '{step.title}' "
                    f"(steps {titles[title]} and {index})"
                )

            titles[title] = index

            if task.change_paths:
                if len(step.change_paths) != 1:
                    errors.append(
                        f"task '{key}' step '{step.title}' must own exactly one file"
                    )
                try:
                    AllowedChangeSet(task.change_paths, step.change_paths)
                except ChangeScopeError as error:
                    errors.append(str(error))
                if not step.verification_specs:
                    errors.append(
                        f"task '{key}' step '{step.title}' has no verification_specs"
                    )

            if not list(step.success_criteria):
                errors.append(
                    f"task '{key}' step '{step.title}' "
                    "has no success_criteria"
                )

            # Dependency consistency: a Step may only require
            # something the Task requires or an earlier Step
            # produced.
            for requirement in step.requires:
                normalized = _normalize(requirement)

                if (
                    normalized not in task_requires
                    and normalized not in produced_so_far
                ):
                    errors.append(
                        f"task '{key}' step '{step.title}' "
                        f"requires unknown resource "
                        f"'{requirement}'"
                    )

            for resource in step.produces:
                produced_so_far.add(
                    _normalize(resource)
                )

            # ONE FILE PATH = ONE STEP OWNER.
            for path in _file_paths(
                list(step.produces)
            ):
                if path in owners:
                    errors.append(
                        f"file '{path}' is produced by two "
                        f"steps: '{owners[path]}' and "
                        f"'{step.title}'"
                    )
                    continue

                owners[path] = step.title

        # Task scope: a Step may not own a file that lives outside the
        # Task's declared files. Test counterparts of a declared file
        # (test_x.py / x_test.py) and files in the same directory are
        # legitimate and therefore allowed.
        task_files = _file_paths(
            list(task.produces)
        )

        if task_files:
            task_stems = {
                Path(path).stem
                for path in task_files
            }

            task_dirs = {
                str(Path(path).parent).replace(
                    "\\",
                    "/",
                )
                for path in task_files
            }

            for path in sorted(owners):
                if path in task_files:
                    continue

                if self._is_test_counterpart(
                    path,
                    task_stems,
                ):
                    continue

                if (
                    str(Path(path).parent).replace(
                        "\\",
                        "/",
                    )
                    in task_dirs
                ):
                    continue

                errors.append(
                    f"step output '{path}' is outside the "
                    f"task '{key}' scope"
                )

        return errors

    def warnings(
        self,
        *,
        task: TaskDraft,
        steps: list[StepDraft],
    ) -> list[str]:
        """
        Non-fatal quality findings (reported, never auto-passed).
        """

        criteria = list(task.success_criteria)

        for step in steps:
            criteria.extend(
                list(step.success_criteria)
            )

        if not criteria:
            return [
                f"task '{task.key}' has no success criteria"
            ]

        if any(
            classify_criterion(criterion) != "unknown"
            for criterion in criteria
        ):
            return []

        return [
            f"task '{task.key}' has no verifiable success "
            "criteria (no file/py_compile/import/pytest check "
            "applies)"
        ]

    @staticmethod
    def _is_test_counterpart(
        path: str,
        stems: set[str],
    ) -> bool:
        stem = Path(path).stem

        return any(
            stem == f"test_{declared}"
            or stem == f"{declared}_test"
            for declared in stems
        )

    def validate_all(
        self,
        tasks: list[TaskDraft],
        steps_by_key: dict[str, list[StepDraft]],
    ) -> list[str]:
        errors: list[str] = []

        for task in tasks:
            errors.extend(
                self.validate(
                    task=task,
                    steps=list(
                        steps_by_key.get(
                            task.key,
                            [],
                        )
                    ),
                )
            )

        return errors

    def warnings_all(
        self,
        tasks: list[TaskDraft],
        steps_by_key: dict[str, list[StepDraft]],
    ) -> list[str]:
        findings: list[str] = []

        for task in tasks:
            findings.extend(
                self.warnings(
                    task=task,
                    steps=list(
                        steps_by_key.get(
                            task.key,
                            [],
                        )
                    ),
                )
            )

        return findings

