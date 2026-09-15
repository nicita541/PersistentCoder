from __future__ import annotations

import json
from pathlib import Path

from app.project_identity import ProjectIdentity
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
from app.tasks.store_context import StoreContext


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
            response = self.default
            # Runtime fixtures model the protocol faithfully: when a
            # canned coder response uses the historical artifact.txt
            # placeholder, bind it to the exact scope shown in the prompt.
            try:
                payload = json.loads(response)
                user_text = "\n".join(
                    str(message.get("content", "")) for message in messages
                )
                marker = "EXACT ALLOWED CHANGE PATHS:\n- "
                if marker in user_text:
                    allowed = user_text.split(marker, 1)[1].splitlines()[0]
                    for entry in payload.get("files", []):
                        if entry.get("path") == "artifact.txt":
                            entry["path"] = allowed
                    response = json.dumps(payload)
            except (AttributeError, TypeError, ValueError):
                pass
            return response

        raise AssertionError(
            "FakeLLM has no response"
        )


class StoreBundle:
    def __init__(self, database_path, *, project_root=None) -> None:
        self.database_path = database_path
        binding = database_path
        if project_root is not None:
            identity = ProjectIdentity.from_source_root(project_root)
            binding = StoreContext(
                database_path=Path(database_path),
                project_id=identity.project_id,
                canonical_source_root=str(identity.canonical_source_root),
            )
        self.plan_store = PlanStore(binding)
        self.step_store = StepStore(binding)
        self.attempt_store = AttemptStore(
            binding
        )
        self.verification_store = (
            VerificationStore(binding)
        )
        self.replan_store = ReplanStore(
            binding
        )


def make_stores(tmp_path, *, project_root=None) -> StoreBundle:
    return StoreBundle(
        tmp_path / "persistent_coder.db",
        project_root=project_root,
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
                        "database.txt exists"
                    ],
                    "change_paths": ["database.txt"],
                    "verification_specs": [
                        {"kind": "FILE_EXISTS", "target": "database.txt"}
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
                        "models.txt exists"
                    ],
                    "change_paths": ["models.txt"],
                    "verification_specs": [
                        {"kind": "FILE_EXISTS", "target": "models.txt"}
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
                        "auth.txt exists"
                    ],
                    "change_paths": ["auth.txt"],
                    "verification_specs": [
                        {"kind": "FILE_EXISTS", "target": "auth.txt"}
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
                    "change_paths": ["artifact.txt"],
                    "verification_specs": [
                        {"kind": "FILE_EXISTS", "target": "artifact.txt"}
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
        self.argv_commands: list[list[str]] = []

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

    def run_argv(self, argv, *, cwd=None):
        from app.tools.terminal_tools import CommandResult

        command = [str(item) for item in argv]
        self.argv_commands.append(command)
        return CommandResult(
            command=" ".join(command),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


