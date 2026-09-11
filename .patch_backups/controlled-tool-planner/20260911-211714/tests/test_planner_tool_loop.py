from __future__ import annotations

import pytest

from app.tasks.planner_tool_loop import (
    ToolPlanner,
    ToolPlannerError,
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
    import json

    return json.dumps(
        {
            "tool": tool,
            "arguments": arguments,
        }
    )


def test_tool_planner_builds_dependencies_from_contracts():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "storage",
                    "title": "Storage",
                    "description": "Create storage.",
                    "priority": 100,
                    "success_criteria": [
                        "Storage works"
                    ],
                },
            ),
            action(
                "create_task",
                {
                    "key": "notes_api",
                    "title": "Notes API",
                    "description": "Create notes API.",
                    "priority": 90,
                    "success_criteria": [
                        "Notes can be created"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "storage",
                    "requires": [],
                    "produces": [
                        "database"
                    ],
                    "external_dependencies": [],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [
                        "database"
                    ],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [
                        "flask"
                    ],
                },
            ),
            action(
                "finish_plan",
                {},
            ),
        ]
    )

    plan = ToolPlanner(
        llm,
        max_tool_steps=10,
    ).plan(
        "Create notes API."
    )

    assert len(plan.tasks) == 2

    storage = plan.tasks[0]
    notes = plan.tasks[1]

    assert storage.key == "storage"
    assert storage.depends_on == []

    assert notes.key == "notes_api"
    assert notes.depends_on == [
        "storage"
    ]

    assert notes.external_dependencies == [
        "flask"
    ]


def test_invalid_finish_returns_error_and_model_can_repair_plan():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "notes_api",
                    "title": "Notes API",
                    "description": "Create notes API.",
                    "success_criteria": [
                        "Notes can be created"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [
                        "database"
                    ],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [
                        "flask"
                    ],
                },
            ),
            action(
                "finish_plan",
                {},
            ),
            action(
                "create_task",
                {
                    "key": "storage",
                    "title": "Storage",
                    "description": "Create storage.",
                    "success_criteria": [
                        "Storage works"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "storage",
                    "requires": [],
                    "produces": [
                        "database"
                    ],
                    "external_dependencies": [],
                },
            ),
            action(
                "finish_plan",
                {},
            ),
        ]
    )

    planner = ToolPlanner(
        llm,
        max_tool_steps=10,
    )

    plan = planner.plan(
        "Create notes API."
    )

    notes = next(
        task
        for task in plan.tasks
        if task.key == "notes_api"
    )

    assert notes.depends_on == [
        "storage"
    ]

    combined_messages = "\n".join(
        message["content"]
        for call in llm.calls
        for message in call
        if message["role"] == "user"
    )

    assert "no producer" in combined_messages


def test_external_dependency_never_needs_task_producer():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "api",
                    "title": "API",
                    "description": "Create API.",
                    "success_criteria": [
                        "API works"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "api",
                    "requires": [],
                    "produces": [
                        "API"
                    ],
                    "external_dependencies": [
                        "flask",
                        "pytest",
                    ],
                },
            ),
            action(
                "finish_plan",
                {},
            ),
        ]
    )

    plan = ToolPlanner(
        llm
    ).plan(
        "Create API."
    )

    task = plan.tasks[0]

    assert task.depends_on == []

    assert task.external_dependencies == [
        "flask",
        "pytest",
    ]


def test_model_cannot_set_depends_on_directly():
    llm = FakeLLM(
        [
            action(
                "create_task",
                {
                    "key": "api",
                    "title": "API",
                    "description": "Create API.",
                    "success_criteria": [
                        "API works"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "api",
                    "requires": [],
                    "produces": [
                        "API"
                    ],
                    "external_dependencies": [],
                    "depends_on": [
                        "imaginary"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "api",
                    "requires": [],
                    "produces": [
                        "API"
                    ],
                    "external_dependencies": [],
                },
            ),
            action(
                "finish_plan",
                {},
            ),
        ]
    )

    planner = ToolPlanner(
        llm,
        max_tool_steps=10,
    )

    plan = planner.plan(
        "Create API."
    )

    assert plan.tasks[0].depends_on == []

    messages = "\n".join(
        message["content"]
        for call in llm.calls
        for message in call
        if message["role"] == "user"
    )

    assert (
        "unexpected arguments: depends_on"
        in messages
    )


def test_unknown_tool_can_be_corrected():
    llm = FakeLLM(
        [
            action(
                "make_everything",
                {},
            ),
            action(
                "create_task",
                {
                    "key": "api",
                    "title": "API",
                    "description": "Create API.",
                    "success_criteria": [
                        "API works"
                    ],
                },
            ),
            action(
                "set_task_contract",
                {
                    "task_key": "api",
                    "requires": [],
                    "produces": [
                        "API"
                    ],
                    "external_dependencies": [],
                },
            ),
            action(
                "finish_plan",
                {},
            ),
        ]
    )

    plan = ToolPlanner(
        llm,
        max_tool_steps=10,
    ).plan(
        "Create API."
    )

    assert len(plan.tasks) == 1


def test_tool_step_limit_is_enforced():
    llm = FakeLLM(
        [
            action(
                "inspect_plan",
                {},
            ),
            action(
                "inspect_plan",
                {},
            ),
            action(
                "inspect_plan",
                {},
            ),
        ]
    )

    with pytest.raises(
        ToolPlannerError,
        match="exceeded 3 tool steps",
    ):
        ToolPlanner(
            llm,
            max_tool_steps=3,
        ).plan(
            "Create API."
        )
