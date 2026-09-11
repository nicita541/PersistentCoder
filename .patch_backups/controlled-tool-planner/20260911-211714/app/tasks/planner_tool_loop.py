from __future__ import annotations

import json
from typing import Any, Protocol

from app.tasks.models import PlanDraft
from app.tasks.planner_tools import (
    PlannerToolRouter,
    PlannerWorkspace,
)


class PlannerLLM(Protocol):
    def chat(
        self,
        messages: list[
            dict[str, str]
        ],
        max_new_tokens: int = 512,
    ) -> str:
        ...


class ToolPlannerError(
    RuntimeError
):
    pass


class ToolPlanner:
    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_tool_steps: int = 24,
    ) -> None:
        if max_tool_steps < 1:
            raise ValueError(
                "max_tool_steps "
                "must be at least 1"
            )

        self.llm = llm
        self.max_tool_steps = (
            max_tool_steps
        )

    def plan(
        self,
        user_request: str,
    ) -> PlanDraft:
        request = user_request.strip()

        if not request:
            raise ToolPlannerError(
                "user request is required"
            )

        workspace = PlannerWorkspace(
            user_request=request,
        )

        router = PlannerToolRouter(
            workspace
        )

        messages = self._initial_messages(
            request
        )

        for _ in range(
            self.max_tool_steps
        ):
            response = self.llm.chat(
                messages,
                max_new_tokens=384,
            )

            messages.append(
                {
                    "role": "assistant",
                    "content": response,
                }
            )

            try:
                action = (
                    self._parse_action(
                        response
                    )
                )

            except ToolPlannerError as error:
                messages.append(
                    self._tool_result_message(
                        {
                            "ok": False,
                            "error": str(error),
                        }
                    )
                )
                continue

            tool_name = action["tool"]
            arguments = action["arguments"]

            result = router.execute(
                tool_name,
                arguments,
            )

            messages.append(
                self._tool_result_message(
                    result
                )
            )

            if (
                tool_name == "finish_plan"
                and result.get("ok")
                and result.get("finished")
            ):
                return (
                    workspace.to_plan_draft()
                )

        raise ToolPlannerError(
            "planner exceeded "
            f"{self.max_tool_steps} "
            "tool steps without "
            "finishing a valid plan"
        )

    @staticmethod
    def _initial_messages(
        user_request: str,
    ) -> list[dict[str, str]]:
        system = """
You are a coding planner operating through tools.

You MUST return exactly ONE JSON tool action per turn.
Do not use Markdown.
Do not explain outside JSON.
Never invent or set depends_on. The system calculates dependencies.

Action format:
{
  "tool": "tool_name",
  "arguments": {}
}

Available tools:

1. create_task
arguments:
{
  "key": "lowercase_key",
  "title": "short title",
  "description": "what this task accomplishes",
  "priority": 0-100,
  "success_criteria": ["verifiable criterion"]
}

2. update_task
arguments may contain:
{
  "task_key": "key",
  "title": "optional",
  "description": "optional",
  "priority": 0-100,
  "success_criteria": ["optional"]
}

3. set_task_contract
arguments:
{
  "task_key": "key",
  "requires": ["internal resource"],
  "produces": ["internal resource"],
  "external_dependencies": ["library/framework/package"]
}

requires contains ONLY resources produced by another task.
produces contains ONLY internal resources this task creates.
external_dependencies contains Flask, FastAPI, pytest, SDKs, packages,
frameworks, external software, and similar dependencies.

4. inspect_plan
arguments: {}

5. validate_plan
arguments: {}

6. remove_task
arguments:
{
  "task_key": "key"
}

7. finish_plan
arguments: {}

Recommended process:
- create a small set of coarse tasks;
- set contracts for those tasks;
- call validate_plan;
- fix every reported error;
- call finish_plan only when the plan is valid.

If a tool reports an error, use the error to choose the next corrective tool.
Do not repeat the same invalid action.
""".strip()

        user = (
            "Build a software implementation "
            "plan for this request:\n\n"
            f"{user_request}"
        )

        return [
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": user,
            },
        ]

    @staticmethod
    def _tool_result_message(
        result: dict[str, Any],
    ) -> dict[str, str]:
        payload = json.dumps(
            result,
            ensure_ascii=False,
        )

        return {
            "role": "user",
            "content": (
                "TOOL_RESULT:\n"
                f"{payload}\n\n"
                "Return exactly one next "
                "JSON tool action."
            ),
        }

    @classmethod
    def _parse_action(
        cls,
        text: str,
    ) -> dict[str, Any]:
        data = cls._parse_json_object(
            text
        )

        tool_name = data.get(
            "tool"
        )

        arguments = data.get(
            "arguments"
        )

        if (
            not isinstance(
                tool_name,
                str,
            )
            or not tool_name.strip()
        ):
            raise ToolPlannerError(
                "'tool' must be "
                "a non-empty string"
            )

        if not isinstance(
            arguments,
            dict,
        ):
            raise ToolPlannerError(
                "'arguments' must be "
                "a JSON object"
            )

        return {
            "tool": tool_name.strip(),
            "arguments": arguments,
        }

    @staticmethod
    def _parse_json_object(
        text: str,
    ) -> dict[str, Any]:
        decoder = json.JSONDecoder()

        for index, char in enumerate(
            text
        ):
            if char != "{":
                continue

            try:
                value, _ = (
                    decoder.raw_decode(
                        text[index:]
                    )
                )

            except json.JSONDecodeError:
                continue

            if isinstance(
                value,
                dict,
            ):
                return value

        raise ToolPlannerError(
            "model did not return "
            "a valid JSON object"
        )
