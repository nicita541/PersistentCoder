from __future__ import annotations

from collections.abc import Iterable

from app.sandbox.project_path import ProjectPath, ProjectPathError


class ChangeScopeError(ValueError):
    pass


def canonicalize_change_paths(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        try:
            path = ProjectPath.parse(value)
        except ProjectPathError as error:
            raise ChangeScopeError(str(error)) from error
        if path.comparison_key in seen:
            continue
        seen.add(path.comparison_key)
        result.append(path.value)
    return result


class AllowedChangeSet:
    def __init__(
        self,
        task_paths: Iterable[str],
        step_paths: Iterable[str] | None = None,
    ) -> None:
        canonical_task = tuple(
            ProjectPath.parse(value)
            for value in canonicalize_change_paths(task_paths)
        )
        canonical_step = tuple(
            ProjectPath.parse(value)
            for value in canonicalize_change_paths(
                step_paths if step_paths is not None else canonical_task
            )
        )
        task_keys = {path.comparison_key for path in canonical_task}
        if any(path.comparison_key not in task_keys for path in canonical_step):
            raise ChangeScopeError("Step change scope must be a subset of Task scope")
        self.task_paths = canonical_task
        self.step_paths = canonical_step
        self._active = {path.comparison_key for path in canonical_step}

    def _assert(self, value: str, operation: str) -> ProjectPath:
        try:
            path = ProjectPath.parse(value)
        except ProjectPathError as error:
            raise ChangeScopeError(str(error)) from error
        if path.comparison_key not in self._active:
            raise ChangeScopeError(
                f"{operation} is outside the exact Step change scope: {path.value}"
            )
        return path

    def assert_can_read(self, value: str) -> ProjectPath:
        return self._assert(value, "read")

    def assert_can_write(self, value: str) -> ProjectPath:
        return self._assert(value, "write")

    def assert_can_delete(self, value: str) -> ProjectPath:
        return self._assert(value, "delete")


def validate_unique_task_paths(tasks: Iterable[object]) -> None:
    owners: dict[str, str] = {}
    for index, task in enumerate(tasks, start=1):
        owner = str(getattr(task, "key", None) or f"task_{index}")
        paths = canonicalize_change_paths(getattr(task, "change_paths", []))
        for value in paths:
            path = ProjectPath.parse(value)
            previous = owners.get(path.comparison_key)
            if previous is not None and previous != owner:
                raise ChangeScopeError(
                    f"project path is owned by multiple Tasks: {path.value} "
                    f"({previous}, {owner})"
                )
            owners[path.comparison_key] = owner
