from __future__ import annotations

from app.agent.planner.agent import PlannerAgent
from app.tasks.models import PlanDraft, TaskDraft
from app.tasks.store import PlanStore


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:
        if not self.responses:
            raise AssertionError("FakeLLM has no response")
        return self.responses.pop(0)


def test_external_dependency_does_not_need_task_producer(
    tmp_path,
):
    llm = FakeLLM(
        [
            """
            {
              "global_goal": "Create notes REST API.",
              "constraints": [],
              "assumptions": [],
              "success_criteria": ["API works"]
            }
            """,
            """
            {
              "tasks": [
                {
                  "key": "notes_api",
                  "title": "Create notes API",
                  "description": "Create REST API.",
                  "priority": 80,
                  "requires": [],
                  "external_dependencies": ["flask"],
                  "produces": ["notes API"],
                  "success_criteria": ["API creates notes"]
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

    planner = PlannerAgent(
        llm,
        PlanStore(
            database_path=tmp_path / "planner.db"
        ),
        max_repair_attempts=2,
    )

    plan = planner.build_draft("Create notes REST API.")

    assert len(plan.tasks) == 1
    task = plan.tasks[0]

    assert task.requires == []
    assert task.external_dependencies == ["flask"]
    assert task.depends_on == []


def test_external_dependencies_survive_database_round_trip(tmp_path):
    database_path = tmp_path / "planner.db"

    store = PlanStore(
        database_path=database_path
    )

    plan_id = store.create_plan(
        PlanDraft(
            user_request="Create API.",
            global_goal="Create API.",
            tasks=[
                TaskDraft(
                    key="api",
                    title="Create API",
                    description="Create API.",
                    external_dependencies=[
                        "flask",
                        "pytest",
                    ],
                )
            ],
        )
    )

    restarted = PlanStore(
        database_path=database_path
    )

    task = restarted.get_tasks(plan_id)[0]

    assert task.external_dependencies == [
        "flask",
        "pytest",
    ]
