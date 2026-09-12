from __future__ import annotations

import json

import pytest

from app.agent.planner.agent import PlannerAgent
from app.agent.planner.contract_builder import (
    ContractBuilder,
)
from app.agent.planner.decomposer import PlannerError
from app.agent.planner.task_builder import TaskBuilder
from app.tasks.models import TaskDraft

from helpers import (
    FakeLLM,
    dependencies_response,
    goal_response,
    make_stores,
    tasks_response,
)


def _planner(
    tmp_path,
    responses,
    **kwargs,
):
    stores = make_stores(tmp_path)

    planner = PlannerAgent(
        FakeLLM(responses),
        stores.plan_store,
        step_store=stores.step_store,
        **kwargs,
    )

    return planner, stores


def test_builds_plan_from_three_passes(tmp_path):
    planner, _ = _planner(
        tmp_path,
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
    )

    result = planner.plan(
        "Сделай API заметок с авторизацией."
    )

    assert result.plan_id is not None

    draft = result.draft

    assert draft.global_goal == (
        "Создать REST API заметок "
        "с авторизацией."
    )

    assert len(draft.tasks) == 4

    database, models, auth, notes_api = (
        draft.tasks
    )

    assert database.key == "database"
    assert models.depends_on == ["database"]
    assert auth.depends_on == ["models"]

    assert notes_api.depends_on == [
        "models",
        "auth",
    ]

    assert len(planner.llm.calls) == 3


def test_plan_is_persisted_in_task_os_with_steps(
    tmp_path,
):
    planner, stores = _planner(
        tmp_path,
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
    )

    result = planner.plan("Сделай API.")

    plan = stores.plan_store.get_plan(
        result.plan_id
    )

    assert plan is not None

    tasks = stores.plan_store.get_tasks(
        result.plan_id
    )

    assert len(tasks) == 4

    for task in tasks:
        assert stores.step_store.get_steps(
            task.id
        ), f"no steps for task {task.id}"


def test_task_count_is_limited(tmp_path):
    many = {
        "tasks": [
            {
                "key": f"task_{index}",
                "title": f"Task {index}",
                "description": "Task.",
                "priority": 50,
                "requires": [],
                "produces": [],
                "success_criteria": ["Done"],
            }
            for index in range(11)
        ]
    }

    planner, _ = _planner(
        tmp_path,
        [goal_response(), json.dumps(many)],
    )

    with pytest.raises(
        PlannerError,
        match="1..10",
    ):
        planner.plan("Большая задача.")


def test_invalid_goal_json_is_repaired(tmp_path):
    planner, _ = _planner(
        tmp_path,
        [
            "это не json",
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        max_repair_attempts=2,
    )

    result = planner.plan("Создай API.")

    assert result.plan_id is not None
    assert len(planner.llm.calls) == 4


def test_invalid_dependency_graph_is_repaired(
    tmp_path,
):
    broken = json.dumps(
        {
            "dependencies": {
                "database": [],
                "models": ["database"],
            }
        }
    )

    planner, _ = _planner(
        tmp_path,
        [
            goal_response(),
            tasks_response(),
            broken,
            dependencies_response(),
        ],
        max_repair_attempts=2,
    )

    result = planner.plan("Создай API.")

    assert len(result.draft.tasks) == 4


def test_contract_rejects_missing_producer():
    builder = ContractBuilder()

    tasks = [
        TaskDraft(
            key="api",
            title="API",
            description="Build API.",
            requires=["database connection"],
            produces=["notes API"],
            success_criteria=["API works"],
        )
    ]

    with pytest.raises(
        PlannerError,
        match="no producer",
    ):
        builder.validate_contracts(tasks)


def test_contract_rejects_multiple_producers():
    builder = ContractBuilder()

    tasks = [
        TaskDraft(
            key="db1",
            title="DB 1",
            description="First.",
            produces=["database"],
            success_criteria=["done"],
        ),
        TaskDraft(
            key="db2",
            title="DB 2",
            description="Second.",
            produces=["database"],
            success_criteria=["done"],
        ),
        TaskDraft(
            key="api",
            title="API",
            description="Uses db.",
            requires=["database"],
            produces=["notes API"],
            success_criteria=["done"],
        ),
    ]

    with pytest.raises(
        PlannerError,
        match="multiple producers",
    ):
        builder.validate_contracts(tasks)


def test_task_builder_requires_success_criteria():
    with pytest.raises(
        PlannerError,
        match="success_criteria",
    ):
        TaskBuilder().build(
            [
                {
                    "key": "api",
                    "title": "API",
                    "description": "Build.",
                    "requires": [],
                    "produces": [],
                    "success_criteria": [],
                }
            ]
        )


# ==========================================
# PLAN-LEVEL BOUNDED REPAIR
# ==========================================


def _duplicate_producer_tasks() -> str:
    """Two tasks produce the same file path -> multiple producers."""

    return json.dumps(
        {
            "tasks": [
                {
                    "key": "api",
                    "title": "Calculator API",
                    "description": "Build the API.",
                    "priority": 80,
                    "requires": [],
                    "produces": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "success_criteria": ["api works"],
                },
                {
                    "key": "cli",
                    "title": "Calculator CLI",
                    "description": "Build the CLI.",
                    "priority": 70,
                    "requires": [],
                    "produces": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "success_criteria": ["cli works"],
                },
                {
                    "key": "tests",
                    "title": "Calculator tests",
                    "description": "Write tests.",
                    "priority": 60,
                    "requires": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "produces": ["calculator test report"],
                    "success_criteria": ["tests pass"],
                },
            ]
        },
        ensure_ascii=False,
    )


def _repaired_tasks() -> str:
    """One file path -> one implementation task owner."""

    return json.dumps(
        {
            "tasks": [
                {
                    "key": "calculator",
                    "title": "Calculator implementation",
                    "description": (
                        "Implement "
                        "sandbox_agent_test/calculator.py."
                    ),
                    "priority": 80,
                    "requires": [],
                    "produces": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "success_criteria": [
                        "calculator.py exists"
                    ],
                },
                {
                    "key": "tests",
                    "title": "Calculator tests",
                    "description": (
                        "Test the calculator module."
                    ),
                    "priority": 70,
                    "requires": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "produces": ["calculator test report"],
                    "success_criteria": ["tests pass"],
                },
            ]
        },
        ensure_ascii=False,
    )


def _repaired_dependencies() -> str:
    return json.dumps(
        {
            "dependencies": {
                "calculator": [],
                "tests": ["calculator"],
            }
        }
    )


def test_plan_level_repair_fixes_multiple_producers(
    tmp_path,
):
    stores = make_stores(tmp_path)

    llm = FakeLLM(
        [
            goal_response(),
            _duplicate_producer_tasks(),
            _repaired_tasks(),
            _repaired_dependencies(),
        ]
    )

    planner = PlannerAgent(
        llm,
        stores.plan_store,
        step_store=stores.step_store,
        max_plan_repairs=2,
    )

    result = planner.plan(
        "Напиши calculator с тестами."
    )

    keys = [
        task.key
        for task in result.draft.tasks
    ]

    assert keys == ["calculator", "tests"]

    assert result.draft.tasks[1].depends_on == [
        "calculator"
    ]

    # goal + rejected decomposition + repaired decomposition + deps
    assert len(llm.calls) == 4


def test_plan_level_repair_is_bounded(tmp_path):
    stores = make_stores(tmp_path)

    llm = FakeLLM(
        [
            goal_response(),
            _duplicate_producer_tasks(),
            _duplicate_producer_tasks(),
            _duplicate_producer_tasks(),
        ]
    )

    planner = PlannerAgent(
        llm,
        stores.plan_store,
        step_store=stores.step_store,
        max_plan_repairs=1,
    )

    with pytest.raises(
        PlannerError,
        match="plan repair failed",
    ):
        planner.plan("Напиши calculator.")

    # goal + two decomposition attempts, then bounded stop
    assert len(llm.calls) == 3


# ==========================================
# REAL TASK -> STEP DECOMPOSITION
# ==========================================


def test_planner_builds_real_steps_per_task(tmp_path):
    stores = make_stores(tmp_path)

    tasks = json.dumps(
        {
            "tasks": [
                {
                    "key": "calculator",
                    "title": "Calculator module",
                    "description": (
                        "Create the calculator module."
                    ),
                    "priority": 80,
                    "requires": [],
                    "produces": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "external_dependencies": [],
                    "success_criteria": [
                        "sandbox_agent_test/"
                        "calculator.py exists"
                    ],
                    "steps": [
                        {
                            "title": "Inspect or create module",
                            "description": "Look at or create the file.",
                            "requires": [],
                            "produces": [],
                            "success_criteria": [
                                "module considered"
                            ],
                        },
                        {
                            "title": "Implement behavior",
                            "description": "Write add and subtract.",
                            "requires": [],
                            "produces": [
                                "sandbox_agent_test/"
                                "calculator.py"
                            ],
                            "success_criteria": [
                                "functions implemented"
                            ],
                        },
                        {
                            "title": "Validate module",
                            "description": "Check it imports.",
                            "requires": [
                                "sandbox_agent_test/"
                                "calculator.py"
                            ],
                            "produces": [],
                            "success_criteria": [
                                "module imports"
                            ],
                        },
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    dependencies = json.dumps(
        {"dependencies": {"calculator": []}}
    )

    planner = PlannerAgent(
        FakeLLM([goal_response(), tasks, dependencies]),
        stores.plan_store,
        step_store=stores.step_store,
        max_plan_repairs=2,
    )

    result = planner.plan("Сделай calculator.")

    task = stores.plan_store.get_tasks(
        result.plan_id
    )[0]

    steps = stores.step_store.get_steps(task.id)

    assert len(steps) == 3
    assert steps[0].title == "Inspect or create module"
    assert steps[1].produces == [
        "sandbox_agent_test/calculator.py"
    ]
    assert steps[2].requires == [
        "sandbox_agent_test/calculator.py"
    ]


