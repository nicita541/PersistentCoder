from __future__ import annotations

import json

import pytest

from app.tasks.planner_tool_loop import (
    ToolPlanner,
)
from app.tasks.planner_tools import (
    PlannerToolError,
    PlannerWorkspace,
)


class FakeLLM:
    def __init__(
        self,
        responses: list[str],
    ) -> None:
        self.responses = list(
            responses
        )
        self.calls: list[
            list[dict[str, str]]
        ] = []

    def chat(
        self,
        messages: list[
            dict[str, str]
        ],
        max_new_tokens: int = 512,
    ) -> str:
        self.calls.append(
            list(messages)
        )

        if not self.responses:
            raise AssertionError(
                "FakeLLM has no response"
            )

        return self.responses.pop(0)


def action(
    tool: str,
    arguments: dict,
) -> str:
    return json.dumps(
        {
            "tool": tool,
            "arguments": arguments,
        }
    )


def add_task(
    workspace: PlannerWorkspace,
    *,
    key: str,
    title: str,
    description: str,
) -> None:
    workspace.create_task(
        key=key,
        title=title,
        description=description,
        priority=50,
        success_criteria=[
            "Result is verifiable"
        ],
    )


def test_exact_title_duplicate_is_rejected_even_when_description_changes():
    workspace = PlannerWorkspace(
        user_request="Create notes API."
    )

    add_task(
        workspace,
        key="api",
        title="Create Notes REST API",
        description=(
            "Implement Flask CRUD routes "
            "for notes."
        ),
    )

    with pytest.raises(
        PlannerToolError,
        match="exact normalized title",
    ):
        add_task(
            workspace,
            key="api_second",
            title="  create   notes rest api  ",
            description=(
                "Write unrelated deployment "
                "documentation and examples."
            ),
        )


def test_move_requirement_to_external_changes_only_dependency_class():
    workspace = PlannerWorkspace(
        user_request="Create notes API."
    )

    add_task(
        workspace,
        key="api",
        title="Create Notes API",
        description="Implement API.",
    )

    workspace.set_task_contract(
        task_key="api",
        requires=[
            "database",
            "Flask",
        ],
        produces=[
            "notes API"
        ],
        external_dependencies=[
            "requests"
        ],
    )

    result = (
        workspace
        .move_requirement_to_external(
            task_key="api",
            resource="flask",
        )
    )

    task = workspace.inspect_plan()[
        "tasks"
    ][0]

    assert result["ok"] is True
    assert task["requires"] == [
        "database"
    ]
    assert task["produces"] == [
        "notes API"
    ]
    assert (
        task["external_dependencies"]
        == [
            "requests",
            "Flask",
        ]
    )


def test_missing_external_dependency_repair_uses_small_mutation_tool():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "notes_api",
                    "title": "Notes API",
                    "description": (
                        "Build the REST API."
                    ),
                    "priority": 80,
                    "success_criteria": [
                        "API works"
                    ],
                },
            ),
            action(
                "finish_task_creation",
                {},
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [
                        "flask"
                    ],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [],
                },
            ),
            action(
                "move_requirement_to_external",
                {
                    "task_key": "notes_api",
                    "resource": "flask",
                },
            ),
        ]
    )

    plan = ToolPlanner(
        llm
    ).plan(
        "Create notes API."
    )

    task = plan.tasks[0]

    assert task.requires == []
    assert (
        task.external_dependencies
        == ["flask"]
    )
    assert task.produces == [
        "notes API"
    ]


def test_two_duplicate_rejections_auto_finish_task_creation():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "api",
                    "title": (
                        "Create Notes REST API"
                    ),
                    "description": (
                        "Implement Flask CRUD "
                        "routes for notes."
                    ),
                    "priority": 80,
                    "success_criteria": [
                        "API works"
                    ],
                },
            ),
            action(
                "create_task",
                {
                    "key": "api_copy_one",
                    "title": (
                        "Create Notes REST API"
                    ),
                    "description": (
                        "Write deployment docs "
                        "for a remote service."
                    ),
                    "priority": 50,
                    "success_criteria": [
                        "Docs exist"
                    ],
                },
            ),
            action(
                "create_task",
                {
                    "key": "api_copy_two",
                    "title": (
                        "Create Notes REST API"
                    ),
                    "description": (
                        "Prepare unrelated "
                        "benchmark examples."
                    ),
                    "priority": 50,
                    "success_criteria": [
                        "Examples exist"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "api",
                    "requires": [],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [
                        "flask"
                    ],
                },
            ),
        ]
    )

    plan = ToolPlanner(
        llm,
        max_duplicate_rejections=2,
    ).plan(
        "Create notes API."
    )

    assert len(plan.tasks) == 1
    assert plan.tasks[0].key == "api"


def test_safe_action_alias_normalization_requires_real_arguments():
    raw = json.dumps(
        {
            "action": (
                "finish_task_creation"
            ),
            "arguments": {},
        }
    )

    parsed = ToolPlanner._parse_action(
        raw
    )

    assert (
        parsed["tool"]
        == "finish_task_creation"
    )
    assert parsed["arguments"] == {}


def test_strict_quality_gate_rejects_task_without_output():
    workspace = PlannerWorkspace(
        user_request="Create notes API."
    )

    add_task(
        workspace,
        key="api",
        title="Create API",
        description="Implement API.",
    )

    quality = (
        workspace.validate_plan_quality(
            strict=True
        )
    )

    assert quality["ok"] is False
    assert any(
        "no declared produced resource"
        in error
        for error in quality["errors"]
    )


def test_strict_quality_gate_rejects_duplicate_outputs():
    workspace = PlannerWorkspace(
        user_request="Create notes API."
    )

    add_task(
        workspace,
        key="api",
        title="Create API",
        description="Implement API.",
    )

    add_task(
        workspace,
        key="docs",
        title="Create API docs",
        description="Document API.",
    )

    workspace.set_task_contract(
        task_key="api",
        requires=[],
        produces=[
            "api_server"
        ],
        external_dependencies=[],
    )

    workspace.set_task_contract(
        task_key="docs",
        requires=[],
        produces=[
            "api_server"
        ],
        external_dependencies=[],
    )

    quality = (
        workspace.validate_plan_quality(
            strict=True
        )
    )

    assert quality["ok"] is False
    assert any(
        "produced by multiple tasks"
        in error
        for error in quality["errors"]
    )
