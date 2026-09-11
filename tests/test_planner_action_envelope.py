from __future__ import annotations

import json

import pytest

from app.tasks.planner_tool_loop import (
    ToolPlanner,
    ToolPlannerError,
)


class RecordingLLM:
    def __init__(
        self,
        responses: list[str],
    ) -> None:
        self.responses = list(responses)
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


def test_parse_action_unwraps_single_nested_tool_action():
    raw = json.dumps(
        {
            "ok": True,
            "global_goal": "Create notes API",
            "tasks": [
                {
                    "tool": "create_task",
                    "arguments": {
                        "key": "api",
                        "title": "API layer",
                        "description": "Build the REST API.",
                        "priority": 80,
                        "success_criteria": [
                            "API routes work"
                        ],
                    },
                }
            ],
        }
    )

    parsed = ToolPlanner._parse_action(
        raw
    )

    assert parsed["tool"] == "create_task"
    assert (
        parsed["arguments"]["key"]
        == "api"
    )


def test_parse_action_rejects_multiple_nested_tool_actions():
    raw = json.dumps(
        {
            "tasks": [
                {
                    "tool": "create_task",
                    "arguments": {
                        "key": "a"
                    },
                },
                {
                    "tool": "create_task",
                    "arguments": {
                        "key": "b"
                    },
                },
            ]
        }
    )

    with pytest.raises(
        ToolPlannerError,
        match="multiple tool actions",
    ):
        ToolPlanner._parse_action(
            raw
        )


def test_task_phase_prompt_does_not_embed_state_envelope():
    llm = RecordingLLM(
        [
            action(
                "create_task",
                {
                    "key": "api",
                    "title": "API layer",
                    "description": "Build REST API.",
                    "priority": 80,
                    "success_criteria": [
                        "API routes work"
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
        llm
    ).plan(
        "Create notes API."
    )

    assert len(plan.tasks) == 1

    first_user_prompt = (
        llm.calls[0][1]["content"]
    )

    assert (
        "CURRENT PLAN STATE"
        not in first_user_prompt
    )
    assert (
        '"ok": true'
        not in first_user_prompt
    )
    assert (
        "CURRENT TASKS:"
        in first_user_prompt
    )
    assert (
        "exactly one JSON"
        in first_user_prompt
    )
