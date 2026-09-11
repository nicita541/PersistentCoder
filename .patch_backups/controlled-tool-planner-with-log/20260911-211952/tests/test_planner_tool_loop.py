from __future__ import annotations

import json

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
    return json.dumps(
        {
            "tool": tool,
            "arguments": arguments,
        }
    )


def create_storage() -> str:
    return action(
        "create_task",
        {
            "key": "storage",
            "title": "Storage",
            "description": (
                "Create persistent storage."
            ),
            "priority": 100,
            "success_criteria": [
                "Storage works"
            ],
        },
    )


def create_api() -> str:
    return action(
        "create_task",
        {
            "key": "notes_api",
            "title": "Notes API",
            "description": (
                "Create notes REST API."
            ),
            "priority": 90,
            "success_criteria": [
                "Notes can be created"
            ],
        },
    )


def finish_tasks() -> str:
    return action(
        "finish_task_creation",
        {},
    )


def storage_contract() -> str:
    return action(
        "set_task_contract",
        {
            "task_key": "storage",
            "requires": [],
            "produces": [
                "database"
            ],
            "external_dependencies": [],
        },
    )


def api_contract() -> str:
    return action(
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
    )


def test_controlled_planner_builds_valid_plan():
    llm = FakeLLM(
        [
            create_storage(),
            create_api(),
            finish_tasks(),
            storage_contract(),
            api_contract(),
        ]
    )

    plan = ToolPlanner(
        llm
    ).plan(
        "Create notes API."
    )

    assert len(plan.tasks) == 2

    storage = plan.tasks[0]
    api = plan.tasks[1]

    assert storage.key == "storage"
    assert storage.depends_on == []

    assert api.key == "notes_api"
    assert api.depends_on == [
        "storage"
    ]

    assert api.external_dependencies == [
        "flask"
    ]


def test_contract_phase_rejects_wrong_tool_and_retries():
    llm = FakeLLM(
        [
            create_api(),
            finish_tasks(),

            action(
                "create_task",
                {
                    "key": "wrong",
                    "title": "Wrong",
                    "description": "Wrong.",
                    "success_criteria": [
                        "Wrong"
                    ],
                },
            ),

            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
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
    assert plan.tasks[0].key == "notes_api"


def test_model_cannot_set_depends_on_directly():
    llm = FakeLLM(
        [
            create_api(),
            finish_tasks(),

            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": [
                        "notes API"
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
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [],
                },
            ),
        ]
    )

    plan = ToolPlanner(
        llm
    ).plan(
        "Create notes API."
    )

    assert (
        plan.tasks[0].depends_on
        == []
    )


def test_missing_producer_can_be_repaired_by_creating_task():
    llm = FakeLLM(
        [
            create_api(),
            finish_tasks(),

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

            create_storage(),

            storage_contract(),
        ]
    )

    plan = ToolPlanner(
        llm
    ).plan(
        "Create notes API."
    )

    assert len(plan.tasks) == 2

    api = next(
        task
        for task in plan.tasks
        if task.key == "notes_api"
    )

    assert api.depends_on == [
        "storage"
    ]


def test_missing_producer_can_be_repaired_by_fixing_contract():
    llm = FakeLLM(
        [
            create_api(),
            finish_tasks(),

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
                    "external_dependencies": [],
                },
            ),

            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [
                        "external database service"
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

    api = plan.tasks[0]

    assert api.depends_on == []
    assert api.external_dependencies == [
        "external database service"
    ]


def test_repeated_identical_action_is_stopped():
    repeated = action(
        "inspect_plan",
        {},
    )

    llm = FakeLLM(
        [
            repeated,
            repeated,
            repeated,
        ]
    )

    with pytest.raises(
        ToolPlannerError,
        match="anti-loop",
    ):
        ToolPlanner(
            llm,
            max_same_action=2,
        ).plan(
            "Create API."
        )


def test_task_phase_can_progress_without_explicit_finish_at_budget():
    llm = FakeLLM(
        [
            create_api(),

            action(
                "create_task",
                {
                    "key": "tests",
                    "title": "Tests",
                    "description": (
                        "Create API tests."
                    ),
                    "success_criteria": [
                        "Tests pass"
                    ],
                },
            ),

            action(
                "set_task_contract",
                {
                    "task_key": "notes_api",
                    "requires": [],
                    "produces": [
                        "notes API"
                    ],
                    "external_dependencies": [
                        "flask"
                    ],
                },
            ),

            action(
                "set_task_contract",
                {
                    "task_key": "tests",
                    "requires": [
                        "notes API"
                    ],
                    "produces": [
                        "verified notes API"
                    ],
                    "external_dependencies": [
                        "pytest"
                    ],
                },
            ),
        ]
    )

    plan = ToolPlanner(
        llm,
        max_task_actions=2,
    ).plan(
        "Create notes API."
    )

    tests = next(
        task
        for task in plan.tasks
        if task.key == "tests"
    )

    assert tests.depends_on == [
        "notes_api"
    ]


def test_empty_request_is_rejected():
    with pytest.raises(
        ToolPlannerError,
        match="request",
    ):
        ToolPlanner(
            FakeLLM([])
        ).plan(
            "   "
        )
