from __future__ import annotations

import json

from app.tasks.attempt_store import AttemptStore
from app.tasks.models import (
    PlanDraft,
    StepDraft,
    TaskDraft,
)
from app.tasks.replan_store import ReplanStore
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.verification_store import (
    VerificationStore,
)


class FakeLLM:
    """
    Один детерминированный LLM для тестов Agent Layer.
    """

    def __init__(
        self,
        responses=None,
        default=None,
    ) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[
            list[dict[str, str]]
        ] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:
        self.calls.append(list(messages))

        if self.responses:
            return self.responses.pop(0)

        if self.default is not None:
            return self.default

        raise AssertionError(
            "FakeLLM has no response"
        )


class StoreBundle:
    def __init__(self, database_path) -> None:
        self.database_path = database_path
        self.plan_store = PlanStore(database_path)
        self.step_store = StepStore(database_path)
        self.attempt_store = AttemptStore(
            database_path
        )
        self.verification_store = (
            VerificationStore(database_path)
        )
        self.replan_store = ReplanStore(
            database_path
        )


def make_stores(tmp_path) -> StoreBundle:
    return StoreBundle(
        tmp_path / "persistent_coder.db"
    )


def seed_plan(
    stores: StoreBundle,
    *,
    key: str = "task1",
    title: str = "Build",
    criteria=None,
    requires=None,
    produces=None,
    with_step: bool = True,
):
    criteria = list(
        criteria or ["a.txt exists"]
    )

    draft = PlanDraft(
        user_request="seed",
        global_goal="seed",
        tasks=[
            TaskDraft(
                key=key,
                title=title,
                description=title,
                requires=list(requires or []),
                produces=list(produces or []),
                success_criteria=criteria,
            )
        ],
    )

    plan_id = stores.plan_store.create_plan(
        draft
    )

    task = stores.plan_store.get_tasks(
        plan_id
    )[0]

    step = None

    if with_step:
        step_ids = (
            stores.step_store.create_steps(
                task.id,
                [
                    StepDraft(
                        title=title,
                        description=title,
                        success_criteria=criteria,
                    )
                ],
            )
        )

        step = stores.step_store.get_step(
            step_ids[0]
        )

    return plan_id, task, step


def goal_response(
    goal: str = (
        "Создать REST API заметок с авторизацией."
    ),
) -> str:
    return json.dumps(
        {
            "global_goal": goal,
            "constraints": [
                "Пользователи должны авторизоваться"
            ],
            "assumptions": [
                "Используется обычный HTTP API"
            ],
            "success_criteria": [
                "Пользователь может войти",
                "Пользователь может создавать заметки",
            ],
        },
        ensure_ascii=False,
    )


def tasks_response() -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "key": "database",
                    "title": "Настроить БД",
                    "description": (
                        "Подготовить слой хранения."
                    ),
                    "priority": 100,
                    "requires": [],
                    "produces": [
                        "database connection"
                    ],
                    "success_criteria": [
                        "artifact.txt exists"
                    ],
                },
                {
                    "key": "models",
                    "title": "Создать модели",
                    "description": "Модели данных.",
                    "priority": 90,
                    "requires": [
                        "database connection"
                    ],
                    "produces": [
                        "User model",
                        "Note model",
                    ],
                    "success_criteria": [
                        "artifact.txt exists"
                    ],
                },
                {
                    "key": "auth",
                    "title": "Создать авторизацию",
                    "description": "Вход пользователя.",
                    "priority": 80,
                    "requires": ["User model"],
                    "produces": [
                        "authentication service"
                    ],
                    "success_criteria": [
                        "artifact.txt exists"
                    ],
                },
                {
                    "key": "notes_api",
                    "title": "Создать API заметок",
                    "description": "Endpoints заметок.",
                    "priority": 70,
                    "requires": [
                        "Note model",
                        "authentication service",
                    ],
                    "produces": ["notes API"],
                    "success_criteria": [
                        "artifact.txt exists"
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )


def dependencies_response() -> str:
    return json.dumps(
        {
            "dependencies": {
                "database": [],
                "models": ["database"],
                "auth": ["models"],
                "notes_api": ["models", "auth"],
            }
        }
    )


def envelope(
    files=None,
    commands=None,
) -> str:
    return json.dumps(
        {
            "files": files or [],
            "commands": commands or [],
        }
    )


def coder_envelope(
    *,
    path: str = "artifact.txt",
    content: str = "hello",
    command: str | None = None,
) -> str:
    commands = [command] if command else []

    return envelope(
        files=[
            {"path": path, "content": content}
        ],
        commands=commands,
    )


class RecordingCommandRunner:
    """
    Test double for the sandbox command runner.

    It never touches the host: it only records commands and
    returns a canned CommandResult.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.commands: list[str] = []

    def run(
        self,
        command: str,
        *,
        cwd=None,
    ):
        from app.tools.terminal_tools import (
            CommandResult,
        )

        self.commands.append(command)

        return CommandResult(
            command=command,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


