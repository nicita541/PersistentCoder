from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from app.tasks.verification_context import VerificationContext


class AgentPhase(str, Enum):
    """
    Фазы Agent Loop.

    Agent Loop реально переключает их последовательно.
    AgentState хранит только ВРЕМЕННОЕ состояние выполнения.
    Долговременные знания живут в app.memory, не здесь.
    """

    PLANNING = "PLANNING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINAL_PHASES = frozenset(
    {
        AgentPhase.DONE,
        AgentPhase.FAILED,
    }
)


def extract_json_object(
    text: str,
) -> dict[str, object]:
    """
    Извлекает первый валидный JSON-объект из ответа модели.

    Единая реализация для всех агентов, чтобы не дублировать
    парсинг модельного вывода.
    """

    decoder = json.JSONDecoder()

    for index, char in enumerate(text):
        if char != "{":
            continue

        try:
            value, _ = decoder.raw_decode(
                text[index:]
            )

        except json.JSONDecodeError:
            continue

        if isinstance(value, dict):
            return value

    raise ValueError(
        "model did not return a valid JSON object"
    )


@dataclass
class CommandExecution:
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def as_evidence(self) -> str:
        output = (
            self.stdout or self.stderr or ""
        ).strip()

        # Debugger needs the actual exception, which pytest often prints after
        # a long collection header. Keep a bounded head and tail instead of
        # truncating away the root cause.
        if len(output) > 1200:
            output = output[:600] + "\n...[output clipped]...\n" + output[-600:]

        evidence = (
            f"command '{self.command}' "
            f"-> rc={self.returncode}"
        )

        if output:
            evidence += f": {output}"

        return evidence


@dataclass
class ExecutionResult:
    """
    Реальный результат CodingAgent.

    Никакого фиктивного status=executed:
    ok=True возможен только когда реально что-то
    применено/выполнено и команды завершились успешно.
    """

    ok: bool
    summary: str = ""
    artifacts: list[str] = field(
        default_factory=list
    )
    evidence: list[str] = field(
        default_factory=list
    )
    commands: list[CommandExecution] = field(
        default_factory=list
    )
    failure_reason: str | None = None

    # Files the agent actually observed (read/listed) during this
    # attempt. Used to enforce observe-before-edit.
    read_files: list[str] = field(
        default_factory=list
    )

    def all_commands_ok(self) -> bool:
        return all(
            command.ok
            for command in self.commands
        )


@dataclass
class CriterionResult:
    """
    Deterministic result for ONE success criterion.

    A criterion is never its own evidence: each one is tied to a real
    check (filesystem, py_compile, import, pytest) or marked BLOCKED.
    """

    criterion: str
    status: str = "BLOCKED"
    check: str = ""
    reason: str = ""
    evidence: list[str] = field(
        default_factory=list
    )


@dataclass
class VerificationResult:
    ok: bool
    status: str = "FAIL"
    reason: str = ""
    evidence: list[str] = field(
        default_factory=list
    )
    criterion_results: list[CriterionResult] = field(
        default_factory=list
    )
    context: VerificationContext | None = None


@dataclass
class RepairState:
    required: bool = False
    action: str | None = None
    scope: str | None = None
    strategy: str | None = None
    reason: str | None = None
    approach: str | None = None
    context: dict[str, object] = field(default_factory=dict)


@dataclass
class AgentState:
    """
    Временное состояние одного выполнения запроса.
    """

    request: str
    phase: AgentPhase = AgentPhase.PLANNING

    global_goal: str | None = None

    plan_id: int | None = None
    active_task_id: int | None = None
    active_step_id: int | None = None

    # Authoritative ids for the current transactional attempt.
    attempt_id: int | None = None
    checkpoint_id: str | None = None

    attempts: dict[str, int] = field(
        default_factory=dict
    )

    execution: ExecutionResult | None = None
    verification: VerificationResult | None = None
    repair: RepairState = field(
        default_factory=RepairState
    )

    completion: str | None = None

    # Path to the sandbox patch produced on DONE (never auto-applied).
    patch_path: str | None = None

    history: list[str] = field(
        default_factory=list
    )

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def record_phase(self) -> None:
        self.history.append(
            self.phase.value
        )
