from __future__ import annotations

import json
from typing import Callable, Protocol, TypeVar

from app.agent.state import extract_json_object
from app.tasks.dependencies import (
    DependencyValidationError,
)


class PlannerLLM(Protocol):
    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:
        ...


class PlannerError(RuntimeError):
    pass


T = TypeVar("T")


# ==========================================
# SHARED JSON / SCHEMA HELPERS
# ==========================================


def parse_json_object(
    text: str,
) -> dict[str, object]:
    try:
        return extract_json_object(text)

    except ValueError as error:
        raise PlannerError(
            "Model did not return "
            "a valid JSON object"
        ) from error


def require_string(
    data: dict[str, object],
    key: str,
) -> str:
    value = data.get(key)

    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise PlannerError(
            f"'{key}' must be "
            "a non-empty string"
        )

    return value.strip()


def require_string_list(
    data: dict[str, object],
    key: str,
) -> list[str]:
    value = data.get(key)

    if not isinstance(value, list):
        raise PlannerError(
            f"'{key}' must be a list"
        )

    result: list[str] = []

    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
        ):
            raise PlannerError(
                f"'{key}' must contain "
                "only non-empty strings"
            )

        result.append(item.strip())

    return result


def optional_string_list(
    data: dict[str, object],
    key: str,
) -> list[str]:
    value = data.get(key, [])

    if not isinstance(value, list):
        raise PlannerError(
            f"'{key}' must be a list"
        )

    result: list[str] = []

    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
        ):
            raise PlannerError(
                f"'{key}' must contain "
                "only non-empty strings"
            )

        result.append(item.strip())

    return result


def normalize_resource(
    value: str,
) -> str:
    return " ".join(
        value.casefold().split()
    )


# ==========================================
# GENERATE + REPAIR
# ==========================================


def build_repair_messages(
    *,
    stage_name: str,
    broken_response: str,
    error: Exception,
    repair_context: str | None = None,
) -> list[dict[str, str]]:
    context_text = ""

    if repair_context:
        context_text = (
            "\n\n"
            "AUTHORITATIVE CONTEXT:\n"
            f"{repair_context}\n"
        )

    return [
        {
            "role": "system",
            "content": (
                "You repair structured JSON "
                "for a coding planner. "
                "Return JSON only. "
                "Do not explain. "
                "Do not use Markdown. "
                "The AUTHORITATIVE CONTEXT "
                "is the source of truth. "
                "Never invent names, task keys "
                "or dependencies that are not "
                "allowed by that context."
            ),
        },
        {
            "role": "user",
            "content": (
                f"STAGE: {stage_name}\n\n"
                "The previous response was "
                "invalid.\n\n"
                "VALIDATION ERROR:\n"
                f"{error}"
                f"{context_text}\n\n"
                "BROKEN RESPONSE:\n"
                f"{broken_response}\n\n"
                "Repair the JSON so that it "
                "passes validation. "
                "Use only values allowed by "
                "AUTHORITATIVE CONTEXT. "
                "Preserve the original intent. "
                "Return exactly one JSON object."
            ),
        },
    ]


def generate_with_repair(
    llm: PlannerLLM,
    *,
    initial_messages: list[dict[str, str]],
    parser: Callable[[str], T],
    max_new_tokens: int,
    stage_name: str,
    max_repair_attempts: int = 0,
    repair_context: str | None = None,
) -> T:
    messages = list(initial_messages)
    last_error: Exception | None = None

    for attempt_index in range(
        max_repair_attempts + 1
    ):
        response = llm.chat(
            messages,
            max_new_tokens=max_new_tokens,
        )

        try:
            return parser(response)

        except (
            PlannerError,
            DependencyValidationError,
        ) as error:
            last_error = error

            if attempt_index >= max_repair_attempts:
                break

            messages = build_repair_messages(
                stage_name=stage_name,
                broken_response=response,
                error=error,
                repair_context=repair_context,
            )

    raise PlannerError(
        f"{stage_name} repair failed "
        f"after {max_repair_attempts} "
        f"repair attempts: {last_error}"
    ) from last_error


# ==========================================
# PASS 1 — GOAL ANALYZER
# ==========================================


class GoalAnalyzer:
    """
    Первый семантический проход Planner.

    LLM анализирует запрос. Python проверяет схему.
    """

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_repair_attempts: int = 0,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError(
                "max_repair_attempts "
                "cannot be negative"
            )

        self.llm = llm
        self.max_repair_attempts = (
            max_repair_attempts
        )

    def analyze(
        self,
        user_request: str,
    ) -> dict[str, object]:
        user_request = user_request.strip()

        if not user_request:
            raise PlannerError(
                "user request is required"
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Goal Analyzer "
                    "inside a coding planner. "
                    "Return JSON only. "
                    "Do not write code. "
                    "Do not execute anything."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Analyze this software "
                    "development request.\n\n"
                    "USER REQUEST:\n"
                    f"{user_request}\n\n"
                    "Return exactly this JSON "
                    "shape:\n"
                    "{\n"
                    '  "global_goal": "string",\n'
                    '  "constraints": ["string"],\n'
                    '  "assumptions": ["string"],\n'
                    '  "success_criteria": '
                    '["string"]\n'
                    "}"
                ),
            },
        ]

        return generate_with_repair(
            self.llm,
            initial_messages=messages,
            parser=self.parse,
            max_new_tokens=384,
            stage_name="GOAL_ANALYZER",
            max_repair_attempts=(
                self.max_repair_attempts
            ),
            repair_context=(
                "USER REQUEST:\n"
                f"{user_request}"
            ),
        )

    @staticmethod
    def parse(
        response: str,
    ) -> dict[str, object]:
        data = parse_json_object(response)

        global_goal = require_string(
            data,
            "global_goal",
        )

        constraints = require_string_list(
            data,
            "constraints",
        )

        assumptions = require_string_list(
            data,
            "assumptions",
        )

        success_criteria = require_string_list(
            data,
            "success_criteria",
        )

        if not success_criteria:
            raise PlannerError(
                "Goal success_criteria "
                "cannot be empty"
            )

        return {
            "global_goal": global_goal,
            "constraints": constraints,
            "assumptions": assumptions,
            "success_criteria": success_criteria,
        }


# ==========================================
# PASS 2 — TASK DECOMPOSER
# ==========================================


class TaskDecomposer:
    """
    Второй семантический проход Planner.

    LLM предлагает semantic components (решения),
    но НЕ создаёт Task и НЕ меняет Task OS.
    Это делает TaskBuilder.
    """

    DECOMPOSER_RULES = (
        "- create 1 to 10 tasks\n"
        "- task keys must be unique\n"
        "- do not create depends_on here\n"
        "- requires contains ONLY internal resources "
        "produced by another task\n"
        "- every requires resource must be produced by "
        "exactly one other task\n"
        "- external libraries, frameworks, packages and "
        "SDKs belong in external_dependencies\n"
        "- Flask, FastAPI, pytest and similar software "
        "must NOT be put in requires\n"
        "- requires and produces names must match exactly\n"
        "- ONE FILE PATH = ONE IMPLEMENTATION TASK OWNER: "
        "a concrete file path (for example "
        "src/calculator.py) may be produced by EXACTLY ONE "
        "task; never let two tasks produce the same file "
        "path and never split a single file across tasks"
    )

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_repair_attempts: int = 0,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError(
                "max_repair_attempts "
                "cannot be negative"
            )

        self.llm = llm
        self.max_repair_attempts = (
            max_repair_attempts
        )

    def decompose(
        self,
        *,
        user_request: str,
        goal: dict[str, object],
        repair_instruction: str | None = None,
        previous_components: (
            list[dict[str, object]] | None
        ) = None,
    ) -> list[dict[str, object]]:
        goal_json = json.dumps(
            goal,
            ensure_ascii=False,
            indent=2,
        )

        repair_block = ""

        if repair_instruction:
            previous_json = ""

            if previous_components:
                previous_json = json.dumps(
                    previous_components,
                    ensure_ascii=False,
                    indent=2,
                )

            repair_block = (
                "REPAIR INSTRUCTION:\n"
                "The previous decomposition was rejected "
                "by the contract validator:\n"
                f"{repair_instruction}\n\n"
                "PREVIOUS (REJECTED) TASKS:\n"
                f"{previous_json}\n\n"
                "Rebuild the FULL task list so that the "
                "rejected contract error cannot occur.\n\n"
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Task Decomposer "
                    "inside a coding planner. "
                    "Return JSON only. "
                    "Create coarse implementation "
                    "tasks. Do not write code."
                ),
            },
            {
                "role": "user",
                "content": (
                    "USER REQUEST:\n"
                    f"{user_request}\n\n"
                    "GOAL ANALYSIS:\n"
                    f"{goal_json}\n\n"
                    f"{repair_block}"
                    "AUTHORITATIVE RULES:\n"
                    f"{self.DECOMPOSER_RULES}\n\n"
                    "VALID EXAMPLE (copy this structure and the "
                    "resource names):\n"
                    '{"tasks": ['
                    '{"key": "impl", "title": "Implement calculator", '
                    '"description": "Implement the module.", '
                    '"priority": 80, "requires": [], '
                    '"produces": ["sandbox_agent_test/calculator.py"], '
                    '"external_dependencies": [], '
                    '"success_criteria": ["calculator.py exists"]}, '
                    '{"key": "tests", "title": "Test calculator", '
                    '"description": "Write pytest tests.", '
                    '"priority": 70, '
                    '"requires": ["sandbox_agent_test/calculator.py"], '
                    '"produces": ["sandbox_agent_test/test_calculator.py"], '
                    '"external_dependencies": ["pytest"], '
                    '"success_criteria": ["tests pass"]}]}\n\n'
                    "IMPORTANT: every value in requires MUST appear "
                    "verbatim in some task's produces. Never put a "
                    "task key or a title into requires.\n\n"
                    "Return exactly this JSON shape:\n"
                    "{\n"
                    '  "tasks": [\n'
                    "    {\n"
                    '      "key": "stable_snake_case",\n'
                    '      "title": "string",\n'
                    '      "description": "string",\n'
                    '      "priority": 50,\n'
                    '      "requires": ["internal resource"],\n'
                    '      "produces": ["internal resource"],\n'
                    '      "external_dependencies": ["flask"],\n'
                    '      "success_criteria": ["verifiable result"]\n'
                    "    }\n"
                    "  ]\n"
                    "}"
                ),
            },
        ]

        return generate_with_repair(
            self.llm,
            initial_messages=messages,
            parser=self.parse,
            max_new_tokens=1024,
            stage_name="TASK_DECOMPOSER",
            max_repair_attempts=(
                self.max_repair_attempts
            ),
            repair_context=(
                "USER REQUEST:\n"
                f"{user_request}\n\n"
                "GOAL ANALYSIS:\n"
                f"{goal_json}\n\n"
                "AUTHORITATIVE RULES:\n"
                f"{self.DECOMPOSER_RULES}"
            ),
        )

    @staticmethod
    def parse(
        response: str,
    ) -> list[dict[str, object]]:
        data = parse_json_object(response)

        raw_tasks = data.get("tasks")

        if not isinstance(raw_tasks, list):
            raise PlannerError(
                "tasks must be a list"
            )

        components: list[
            dict[str, object]
        ] = []

        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                raise PlannerError(
                    "each task must be "
                    "a JSON object"
                )

            components.append(dict(raw_task))

        return components

