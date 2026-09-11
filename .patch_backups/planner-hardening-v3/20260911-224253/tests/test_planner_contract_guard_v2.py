from __future__ import annotations

import json

import pytest

from app.tasks.planner_tool_loop import ToolPlanner
from app.tasks.planner_tools import (
    PlannerToolError,
    PlannerWorkspace,
)


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("FakeLLM has no response")
        return self.responses.pop(0)


def action(tool: str, arguments: dict) -> str:
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
        success_criteria=["Result is verifiable"],
    )


def test_duplicate_gate_rejects_semantic_duplicate():
    workspace = PlannerWorkspace(
        user_request="Create notes API."
    )

    add_task(
        workspace,
        key="note_api",
        title="Create notes REST API",
        description="Build a simple Python REST API for notes.",
    )

    with pytest.raises(
        PlannerToolError,
        match="duplicates existing task",
    ):
        add_task(
            workspace,
            key="note_api_rest",
            title="Create a REST API for notes",
            description="Build a simple Python REST API for notes.",
        )


def test_duplicate_gate_allows_distinct_task():
    workspace = PlannerWorkspace(
        user_request="Create notes API."
    )

    add_task(
        workspace,
        key="note_api",
        title="Create notes REST API",
        description="Build a Python REST API for notes.",
    )

    add_task(
        workspace,
        key="tests",
        title="Test notes API",
        description="Add automated tests for the REST API.",
    )

    assert len(workspace.inspect_plan()["tasks"]) == 2


def test_external_dependency_can_be_repaired_after_validation():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "notes_api",
                    "title": "Notes API",
                    "description": "Build the REST API.",
                    "priority": 80,
                    "success_criteria": ["API works"],
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
                    "requires": ["flask"],
                    "produces": ["notes API"],
                    "external_dependencies": [],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": ["notes API"],
                    "external_dependencies": ["flask"],
                },
            ),
        ]
    )

    plan = ToolPlanner(llm).plan(
        "Create notes API."
    )

    task = plan.tasks[0]

    assert task.requires == []
    assert task.external_dependencies == ["flask"]


def test_malformed_repair_action_gets_one_format_retry():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "notes_api",
                    "title": "Notes API",
                    "description": "Build the REST API.",
                    "priority": 80,
                    "success_criteria": ["API works"],
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
                    "requires": ["flask"],
                    "produces": ["notes API"],
                    "external_dependencies": [],
                },
            ),
            "set_task_contract",
            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": ["notes API"],
                    "external_dependencies": ["flask"],
                },
            ),
        ]
    )

    plan = ToolPlanner(
        llm,
        max_format_retries=1,
    ).plan(
        "Create notes API."
    )

    assert (
        plan.tasks[0].external_dependencies
        == ["flask"]
    )

    retry_prompt = llm.calls[4][1]["content"]

    assert "FORMAT ERROR" in retry_prompt
    assert "Return exactly one JSON object" in retry_prompt


def test_repair_prompt_explains_both_missing_producer_repairs():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "notes_api",
                    "title": "Notes API",
                    "description": "Build the REST API.",
                    "priority": 80,
                    "success_criteria": ["API works"],
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
                    "requires": ["flask"],
                    "produces": ["notes API"],
                    "external_dependencies": [],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": ["notes API"],
                    "external_dependencies": ["flask"],
                },
            ),
        ]
    )

    ToolPlanner(llm).plan(
        "Create notes API."
    )

    repair_prompt = llm.calls[3][1]["content"]

    assert "external_dependencies" in repair_prompt
    assert "create_task" in repair_prompt
    assert "set_task_contract" in repair_prompt
