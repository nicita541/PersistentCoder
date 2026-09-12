from __future__ import annotations

import json

from app.agent.planner.task_builder import TaskBuilder
from app.agent.runtime import AgentRuntime
from app.tasks.models import TaskDraft

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
)


def _task(key, requires, produces, criteria=None):
    return TaskDraft(
        key=key,
        title=key,
        description=f"do {key}",
        requires=list(requires),
        produces=list(produces),
        success_criteria=list(
            criteria or ["artifact.txt exists"]
        ),
    )


def test_sibling_key_in_requires_becomes_dependency():
    tasks = [
        _task("create_module_dir", [], ["directory structure"]),
        _task(
            "write_add_function",
            ["create_module_dir"],
            ["calculator.py"],
        ),
    ]

    normalized = TaskBuilder.normalize_key_requires(tasks)

    second = normalized[1]

    assert second.requires == []
    assert second.depends_on == ["create_module_dir"]

    # Untouched tasks stay identical.
    assert normalized[0] is tasks[0]


def test_real_resource_requirement_is_not_touched():
    tasks = [
        _task("database", [], ["database connection"]),
        _task(
            "api",
            ["database connection"],
            ["notes API"],
        ),
    ]

    normalized = TaskBuilder.normalize_key_requires(tasks)

    assert normalized[1].requires == [
        "database connection"
    ]
    assert normalized[1].depends_on == []


def test_self_reference_is_not_silently_rewritten():
    tasks = [
        _task("solo", ["solo"], ["thing"]),
    ]

    normalized = TaskBuilder.normalize_key_requires(tasks)

    # Left as-is, so the contract validator still rejects it.
    assert normalized[0].requires == ["solo"]


def test_key_requires_decomposition_now_plans_successfully(
    tmp_path,
):
    decomposed = json.dumps(
        {
            "tasks": [
                {
                    "key": "create_module_dir",
                    "title": "Create module dir",
                    "description": "Create the directory.",
                    "priority": 90,
                    "requires": [],
                    "produces": ["directory structure"],
                    "success_criteria": [
                        "artifact.txt exists"
                    ],
                },
                {
                    "key": "write_add_function",
                    "title": "Write add()",
                    "description": "Implement add().",
                    "priority": 80,
                    "requires": ["create_module_dir"],
                    "produces": ["calculator.py"],
                    "success_criteria": [
                        "calculator.py exists"
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )

    dependencies = json.dumps(
        {
            "dependencies": {
                "create_module_dir": [],
                "write_add_function": [],
            }
        }
    )

    llm = FakeLLM(
        [
            goal_response(),
            decomposed,
            dependencies,
        ],
        default=coder_envelope(),
    )

    runtime = AgentRuntime(
        workspace_root=tmp_path,
        database_path=tmp_path / "pc.db",
        llm=llm,
        system_prompt="GLOBAL SYSTEM POLICY",
    )

    state = runtime.run("Создай модуль calculator")

    # The plan was created: the key-in-requires mistake was normalized
    # instead of aborting planning.
    assert state.plan_id is not None

    tasks = runtime.plan_store.get_tasks(state.plan_id)

    by_key = {task.key: task for task in tasks}

    assert by_key["write_add_function"].requires == []

    assert runtime.plan_store.get_task_dependencies(
        by_key["write_add_function"].id
    )
