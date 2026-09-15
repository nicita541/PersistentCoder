from __future__ import annotations

import os

import pytest

from app.tasks.change_scope import (
    AllowedChangeSet,
    ChangeScopeError,
    validate_unique_task_paths,
)
from app.tasks.models import PlanDraft, StepDraft, TaskDraft
from app.tasks.step_store import StepStore, StepStoreError
from app.tasks.store import PlanStore


def _task(key: str, paths: list[str]) -> TaskDraft:
    return TaskDraft(
        key=key,
        title=key,
        description=key,
        success_criteria=["done"],
        change_paths=paths,
    )


def test_exact_step_scope_has_no_same_directory_exception() -> None:
    scope = AllowedChangeSet(
        task_paths=["src/app.py", "src/routes.py"],
        step_paths=["./src/app.py"],
    )

    assert scope.assert_can_write(r"src\app.py").value == "src/app.py"
    with pytest.raises(ChangeScopeError):
        scope.assert_can_write("src/routes.py")
    with pytest.raises(ChangeScopeError):
        scope.assert_can_write("src/sibling.py")


def test_step_scope_must_be_subset_of_task_scope() -> None:
    with pytest.raises(ChangeScopeError, match="subset"):
        AllowedChangeSet(
            task_paths=["src/app.py"],
            step_paths=["tests/test_app.py"],
        )


def test_empty_scope_is_fail_closed() -> None:
    scope = AllowedChangeSet(task_paths=[], step_paths=[])
    with pytest.raises(ChangeScopeError):
        scope.assert_can_delete("anything.py")


def test_task_path_ownership_uses_host_case_rules() -> None:
    tasks = [_task("a", ["SRC/App.py"]), _task("b", ["src/app.py"])]
    if os.name == "nt":
        with pytest.raises(ChangeScopeError, match="owned by multiple"):
            validate_unique_task_paths(tasks)
    else:
        validate_unique_task_paths(tasks)


def test_plan_store_rejects_duplicate_normalized_producer(tmp_path) -> None:
    store = PlanStore(tmp_path / "plans.db")
    plan = PlanDraft(
        user_request="build",
        global_goal="build",
        tasks=[_task("a", ["./src/app.py"]), _task("b", [r"src\app.py"])],
    )

    with pytest.raises(ChangeScopeError, match="owned by multiple"):
        store.create_plan(plan)


def test_task_and_step_paths_survive_round_trip(tmp_path) -> None:
    database = tmp_path / "plans.db"
    plan_store = PlanStore(database)
    plan_id = plan_store.create_plan(
        PlanDraft(
            user_request="build",
            global_goal="build",
            tasks=[_task("a", ["./src/app.py", r"tests\test_app.py"])],
        )
    )
    task = plan_store.get_tasks(plan_id)[0]
    assert task.change_paths == ["src/app.py", "tests/test_app.py"]

    step_store = StepStore(database)
    step_id = step_store.create_steps(
        task.id,
        [
            StepDraft(
                title="implementation",
                description="implementation",
                success_criteria=["done"],
                change_paths=["./src/app.py"],
            )
        ],
    )[0]
    assert step_store.get_step(step_id).change_paths == ["src/app.py"]


def test_step_store_rejects_scope_outside_task(tmp_path) -> None:
    database = tmp_path / "plans.db"
    plan_store = PlanStore(database)
    plan_id = plan_store.create_plan(
        PlanDraft(
            user_request="build",
            global_goal="build",
            tasks=[_task("a", ["src/app.py"])],
        )
    )
    task = plan_store.get_tasks(plan_id)[0]

    with pytest.raises(StepStoreError, match="subset"):
        StepStore(database).create_steps(
            task.id,
            [
                StepDraft(
                    title="escape",
                    description="escape",
                    success_criteria=["done"],
                    change_paths=["src/other.py"],
                )
            ],
        )
