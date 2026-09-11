from __future__ import annotations

import pytest

from app.tasks.ai_planner import (
    AIPlanner,
    AIPlannerError,
)
from app.tasks.models import (
    PlanDraft,
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
            messages
        )

        if not self.responses:
            raise AssertionError(
                "FakeLLM has no response"
            )

        return self.responses.pop(0)


def _valid_responses() -> list[str]:
    return [
        """
        {
          "global_goal":
            "Создать REST API заметок с авторизацией.",
          "constraints": [
            "Пользователи должны авторизоваться"
          ],
          "assumptions": [
            "Используется обычный HTTP API"
          ],
          "success_criteria": [
            "Пользователь может войти",
            "Пользователь может создавать заметки"
          ]
        }
        """,
        """
        {
          "tasks": [
            {
              "key": "database",
              "title": "Настроить БД",
              "description":
                "Подготовить слой хранения данных.",
              "priority": 100,
              "requires": [],
              "produces": [
                "database connection"
              ],
              "success_criteria": [
                "Подключение к БД работает"
              ]
            },
            {
              "key": "models",
              "title": "Создать модели",
              "description":
                "Создать модели пользователя и заметки.",
              "priority": 90,
              "requires": [
                "database connection"
              ],
              "produces": [
                "User model",
                "Note model"
              ],
              "success_criteria": [
                "Модели доступны приложению"
              ]
            },
            {
              "key": "auth",
              "title": "Создать авторизацию",
              "description":
                "Реализовать вход пользователя.",
              "priority": 80,
              "requires": [
                "User model"
              ],
              "produces": [
                "authentication service"
              ],
              "success_criteria": [
                "Пользователь может войти"
              ]
            },
            {
              "key": "notes_api",
              "title": "Создать API заметок",
              "description":
                "Создать защищённые endpoints заметок.",
              "priority": 70,
              "requires": [
                "Note model",
                "authentication service"
              ],
              "produces": [
                "notes API"
              ],
              "success_criteria": [
                "Авторизованный пользователь может создавать заметки"
              ]
            }
          ]
        }
        """,
        """
        {
          "dependencies": {
            "database": [],
            "models": [
              "database"
            ],
            "auth": [
              "models"
            ],
            "notes_api": [
              "models",
              "auth"
            ]
          }
        }
        """,
    ]


def test_ai_planner_builds_plan_from_three_passes():
    llm = FakeLLM(
        _valid_responses()
    )

    planner = AIPlanner(
        llm
    )

    plan = planner.plan(
        "Сделай API заметок "
        "с авторизацией."
    )

    assert isinstance(
        plan,
        PlanDraft,
    )

    assert plan.global_goal == (
        "Создать REST API заметок "
        "с авторизацией."
    )

    assert len(plan.tasks) == 4

    database = plan.tasks[0]
    models = plan.tasks[1]
    auth = plan.tasks[2]
    notes_api = plan.tasks[3]

    assert database.key == "database"

    assert models.depends_on == [
        "database"
    ]

    assert auth.depends_on == [
        "models"
    ]

    assert notes_api.depends_on == [
        "models",
        "auth",
    ]

    assert len(llm.calls) == 3


def test_ai_planner_accepts_json_code_fence():
    responses = (
        _valid_responses()
    )

    responses[0] = """
    Конечно.

    ```json
    {
      "global_goal": "Создать API.",
      "constraints": [],
      "assumptions": [],
      "success_criteria": [
        "API работает"
      ]
    }
    ```
    """

    llm = FakeLLM(
        responses
    )

    planner = AIPlanner(
        llm
    )

    plan = planner.plan(
        "Создай API."
    )

    assert plan.global_goal == (
        "Создать API."
    )


def test_invalid_json_is_rejected():
    llm = FakeLLM(
        [
            "это вообще не json",
        ]
    )

    planner = AIPlanner(
        llm
    )

    with pytest.raises(
        AIPlannerError,
        match="JSON",
    ):
        planner.plan(
            "Создай API."
        )


def test_empty_user_request_is_rejected():
    llm = FakeLLM([])

    planner = AIPlanner(
        llm
    )

    with pytest.raises(
        AIPlannerError,
        match="request",
    ):
        planner.plan(
            "   "
        )


def test_unknown_dependency_is_rejected():
    responses = (
        _valid_responses()
    )

    responses[2] = """
    {
      "dependencies": {
        "database": [],
        "models": [
          "unknown_task"
        ],
        "auth": [
          "models"
        ],
        "notes_api": [
          "auth"
        ]
      }
    }
    """

    planner = AIPlanner(
        FakeLLM(
            responses
        )
    )

    with pytest.raises(
        AIPlannerError,
        match="Invalid task graph",
    ):
        planner.plan(
            "Создай API."
        )


def test_cycle_generated_by_model_is_rejected():
    responses = (
        _valid_responses()
    )

    responses[2] = """
    {
      "dependencies": {
        "database": [
          "notes_api"
        ],
        "models": [
          "database"
        ],
        "auth": [
          "models"
        ],
        "notes_api": [
          "auth"
        ]
      }
    }
    """

    planner = AIPlanner(
        FakeLLM(
            responses
        )
    )

    with pytest.raises(
        AIPlannerError,
        match="Invalid task graph",
    ):
        planner.plan(
            "Создай API."
        )


def test_dependency_builder_must_return_every_task():
    responses = (
        _valid_responses()
    )

    responses[2] = """
    {
      "dependencies": {
        "database": [],
        "models": [
          "database"
        ]
      }
    }
    """

    planner = AIPlanner(
        FakeLLM(
            responses
        )
    )

    with pytest.raises(
        AIPlannerError,
        match="dependencies",
    ):
        planner.plan(
            "Создай API."
        )


def test_task_count_is_limited():
    task_objects = []

    for index in range(11):
        task_objects.append(
            f"""
            {{
              "key": "task_{index}",
              "title": "Task {index}",
              "description": "Task.",
              "priority": 50,
              "requires": [],
              "produces": [],
              "success_criteria": [
                "Done"
              ]
            }}
            """
        )

    tasks_json = (
        '{"tasks": ['
        + ",".join(task_objects)
        + "]}"
    )

    llm = FakeLLM(
        [
            """
            {
              "global_goal": "Goal",
              "constraints": [],
              "assumptions": [],
              "success_criteria": [
                "Done"
              ]
            }
            """,
            tasks_json,
        ]
    )

    planner = AIPlanner(
        llm
    )

    with pytest.raises(
        AIPlannerError,
        match="1..10",
    ):
        planner.plan(
            "Большая задача."
        )

def test_invalid_goal_json_is_repaired():
    responses = _valid_responses()

    llm = FakeLLM(
        [
            "это не json",
            """
            {
              "global_goal": "Создать API.",
              "constraints": [],
              "assumptions": [],
              "success_criteria": [
                "API работает"
              ]
            }
            """,
            responses[1],
            responses[2],
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    plan = planner.plan(
        "Создай API."
    )

    assert plan.global_goal == (
        "Создать API."
    )

    assert len(llm.calls) == 4


def test_invalid_task_json_is_repaired():
    responses = _valid_responses()

    llm = FakeLLM(
        [
            responses[0],
            "сломанный json",
            responses[1],
            responses[2],
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    plan = planner.plan(
        "Создай API."
    )

    assert len(plan.tasks) == 4
    assert len(llm.calls) == 4


def test_invalid_dependency_graph_is_repaired():
    responses = _valid_responses()

    invalid_dependencies = """
    {
      "dependencies": {
        "database": [],
        "models": [
          "unknown"
        ],
        "auth": [
          "models"
        ],
        "notes_api": [
          "auth"
        ]
      }
    }
    """

    llm = FakeLLM(
        [
            responses[0],
            responses[1],
            invalid_dependencies,
            responses[2],
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    plan = planner.plan(
        "Создай API."
    )

    models = next(
        task
        for task in plan.tasks
        if task.key == "models"
    )

    assert models.depends_on == [
        "database"
    ]

    assert len(llm.calls) == 4


def test_repair_limit_is_enforced():
    llm = FakeLLM(
        [
            "bad",
            "still bad",
            "bad again",
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    with pytest.raises(
        AIPlannerError,
        match="repair",
    ):
        planner.plan(
            "Создай API."
        )

    assert len(llm.calls) == 3


def test_repair_prompt_contains_failure_reason():
    llm = FakeLLM(
        [
            "broken json",
            """
            {
              "global_goal": "Создать API.",
              "constraints": [],
              "assumptions": [],
              "success_criteria": [
                "API работает"
              ]
            }
            """,
            _valid_responses()[1],
            _valid_responses()[2],
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    planner.plan(
        "Создай API."
    )

    repair_call = llm.calls[1]

    repair_text = " ".join(
        message["content"]
        for message in repair_call
    )

    assert "JSON" in repair_text

def test_dependency_repair_receives_allowed_task_keys():
    responses = _valid_responses()

    invalid_dependencies = """
    {
      "dependencies": {
        "database": [],
        "models": [
          "database"
        ],
        "auth": [
          "imaginary_database"
        ],
        "notes_api": [
          "auth"
        ]
      }
    }
    """

    llm = FakeLLM(
        [
            responses[0],
            responses[1],
            invalid_dependencies,
            responses[2],
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=1,
    )

    planner.plan(
        "Создай API."
    )

    repair_call = llm.calls[3]

    repair_text = "\n".join(
        message["content"]
        for message in repair_call
    )

    assert "allowed_task_keys" in repair_text

    assert "database" in repair_text
    assert "models" in repair_text
    assert "auth" in repair_text
    assert "notes_api" in repair_text

def test_dependency_builder_falls_back_to_contracts_after_repairs_fail():
    responses = _valid_responses()

    invalid_dependencies = """
    {
      "dependencies": {
        "database": [],
        "models": [
          "imaginary_database"
        ],
        "auth": [
          "imaginary_models"
        ],
        "notes_api": [
          "imaginary_auth"
        ]
      }
    }
    """

    llm = FakeLLM(
        [
            responses[0],
            responses[1],

            # Initial dependency generation.
            invalid_dependencies,

            # Repair #1.
            invalid_dependencies,

            # Repair #2.
            invalid_dependencies,
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    plan = planner.plan(
        "Создай API заметок "
        "с авторизацией."
    )

    database = next(
        task
        for task in plan.tasks
        if task.key == "database"
    )

    models = next(
        task
        for task in plan.tasks
        if task.key == "models"
    )

    auth = next(
        task
        for task in plan.tasks
        if task.key == "auth"
    )

    notes_api = next(
        task
        for task in plan.tasks
        if task.key == "notes_api"
    )

    assert database.depends_on == []

    assert models.depends_on == [
        "database"
    ]

    assert auth.depends_on == [
        "models"
    ]

    assert notes_api.depends_on == [
        "models",
        "auth",
    ]

    assert len(llm.calls) == 5

def test_task_decomposer_repairs_missing_required_producer():
    responses = _valid_responses()

    broken_tasks = """
    {
      "tasks": [
        {
          "key": "create_note",
          "title": "Создать заметки",
          "description": "Создать API заметок.",
          "priority": 80,
          "requires": [
            "database"
          ],
          "produces": [
            "notes API"
          ],
          "success_criteria": [
            "Заметки можно создавать"
          ]
        }
      ]
    }
    """

    llm = FakeLLM(
        [
            responses[0],
            broken_tasks,

            # Repair Task Decomposer.
            responses[1],

            # Dependency Builder.
            responses[2],
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    plan = planner.plan(
        "Создай API заметок "
        "с авторизацией."
    )

    assert len(plan.tasks) == 4
    assert len(llm.calls) == 4

    repair_call = llm.calls[2]

    repair_text = "\n".join(
        message["content"]
        for message in repair_call
    )

    assert (
        "TASK_DECOMPOSER"
        in repair_text
    )

    assert (
        "no producer"
        in repair_text
    )


def test_task_contract_failure_stops_before_dependency_builder():
    responses = _valid_responses()

    broken_tasks = """
    {
      "tasks": [
        {
          "key": "create_note",
          "title": "Создать заметки",
          "description": "Создать API заметок.",
          "priority": 80,
          "requires": [
            "database"
          ],
          "produces": [
            "notes API"
          ],
          "success_criteria": [
            "Заметки можно создавать"
          ]
        }
      ]
    }
    """

    llm = FakeLLM(
        [
            responses[0],

            broken_tasks,
            broken_tasks,
            broken_tasks,
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    with pytest.raises(
        AIPlannerError,
        match="TASK_DECOMPOSER",
    ):
        planner.plan(
            "Создай API заметок."
        )

    # 1 Goal call
    # +
    # 1 initial Task Decomposer
    # +
    # 2 Task repairs
    #
    # Dependency Builder вообще
    # не должен запускаться.
    assert len(llm.calls) == 4

def test_external_dependency_does_not_need_task_producer():
    llm = FakeLLM(
        [
            """
            {
              "global_goal":
                "Создать REST API заметок.",
              "constraints": [],
              "assumptions": [],
              "success_criteria": [
                "API работает"
              ]
            }
            """,
            """
            {
              "tasks": [
                {
                  "key": "notes_api",
                  "title": "Создать API заметок",
                  "description":
                    "Создать REST API.",
                  "priority": 80,
                  "requires": [],
                  "external_dependencies": [
                    "flask"
                  ],
                  "produces": [
                    "notes API"
                  ],
                  "success_criteria": [
                    "API создаёт заметки"
                  ]
                }
              ]
            }
            """,
            """
            {
              "dependencies": {
                "notes_api": []
              }
            }
            """,
        ]
    )

    planner = AIPlanner(
        llm,
        max_repair_attempts=2,
    )

    plan = planner.plan(
        "Создай REST API заметок."
    )

    assert len(plan.tasks) == 1

    task = plan.tasks[0]

    assert task.requires == []

    assert task.external_dependencies == [
        "flask"
    ]

    assert task.depends_on == []