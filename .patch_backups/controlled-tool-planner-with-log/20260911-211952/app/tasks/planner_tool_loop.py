from __future__ import annotations

import json
import re
from typing import Any, Protocol

from app.tasks.models import PlanDraft
from app.tasks.planner_tools import (
    PlannerToolError,
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


class ControlledToolPlanner:
    """
    A phase-controlled planner for small local models.

    The model never gets a free-form list of all tools at once.

    Phase 1:
        create coarse tasks only.

    Phase 2:
        fill one task contract at a time.

    Phase 3:
        deterministic validation / dependency inference.

    Phase 4:
        targeted repair for one concrete validation error.
    """

    _NO_PRODUCER_RE = re.compile(
        r"task '([^']+)' requires resource "
        r"'([^']+)', but it has no producer"
    )

    _MULTIPLE_PRODUCERS_RE = re.compile(
        r"task '([^']+)' requires resource "
        r"'([^']+)', but it has multiple producers:"
    )

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_task_actions: int = 8,
        max_contract_attempts: int = 3,
        max_repair_rounds: int = 4,
        max_same_action: int = 2,
        max_tool_steps: int | None = None,
    ) -> None:
        # Backward compatibility with the previous runtime/tests:
        # max_tool_steps now acts as a coarse upper bound only.
        if max_tool_steps is not None:
            if max_tool_steps < 1:
                raise ValueError(
                    "max_tool_steps must be at least 1"
                )

            max_task_actions = min(
                max_task_actions,
                max_tool_steps,
            )

        if max_task_actions < 1:
            raise ValueError(
                "max_task_actions must be at least 1"
            )

        if max_contract_attempts < 1:
            raise ValueError(
                "max_contract_attempts "
                "must be at least 1"
            )

        if max_repair_rounds < 0:
            raise ValueError(
                "max_repair_rounds "
                "cannot be negative"
            )

        if max_same_action < 1:
            raise ValueError(
                "max_same_action "
                "must be at least 1"
            )

        self.llm = llm
        self.max_task_actions = (
            max_task_actions
        )
        self.max_contract_attempts = (
            max_contract_attempts
        )
        self.max_repair_rounds = (
            max_repair_rounds
        )
        self.max_same_action = (
            max_same_action
        )

        self._same_action_signature: (
            str | None
        ) = None
        self._same_action_count = 0

    # ==========================================================
    # PUBLIC API
    # ==========================================================

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

        self._collect_tasks(
            workspace
        )

        for task in list(
            workspace._tasks.values()
        ):
            assert task.key is not None

            self._collect_contract(
                workspace,
                task.key,
            )

        for repair_round in range(
            self.max_repair_rounds
            + 1
        ):
            validation = (
                workspace.validate_plan()
            )

            if validation.get("ok"):
                return workspace.to_plan_draft()

            errors = validation.get(
                "errors",
                [],
            )

            if not errors:
                raise ToolPlannerError(
                    "plan validation failed "
                    "without an error message"
                )

            if (
                repair_round
                >= self.max_repair_rounds
            ):
                raise ToolPlannerError(
                    "controlled planner could not "
                    "repair plan after "
                    f"{self.max_repair_rounds} "
                    "repair rounds: "
                    f"{errors[0]}"
                )

            self._repair_one_error(
                workspace,
                str(errors[0]),
            )

        raise ToolPlannerError(
            "controlled planner ended "
            "unexpectedly"
        )

    # ==========================================================
    # PHASE 1 — TASK CREATION
    # ==========================================================

    def _collect_tasks(
        self,
        workspace: PlannerWorkspace,
    ) -> None:
        for action_number in range(
            1,
            self.max_task_actions + 1,
        ):
            state = workspace.inspect_plan()

            action = self._ask_action(
                phase="TASKS",
                system_prompt=(
                    "You are in TASK CREATION phase. "
                    "Return exactly one JSON object. "
                    "Only two actions are allowed: "
                    "create_task or finish_task_creation. "
                    "Do not update, remove, validate, "
                    "or create contracts yet. "
                    "Create coarse implementation chunks, "
                    "not user CRUD operations. "
                    "A task is a piece of software work, "
                    "not 'create note #1' or 'update note #1'. "
                    "Before finishing, include any obvious "
                    "infrastructure task that other tasks "
                    "will need, such as storage or an API "
                    "layer, when appropriate. "
                    "Do not copy placeholder words."
                ),
                user_prompt=(
                    "USER REQUEST:\n"
                    f"{workspace.user_request}\n\n"
                    "CURRENT PLAN STATE:\n"
                    + json.dumps(
                        state,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n\n"
                    "Allowed JSON shapes:\n"
                    "{"
                    '"tool":"create_task",'
                    '"arguments":{'
                    '"key":"<unique lowercase key>",'
                    '"title":"<task title>",'
                    '"description":"<implementation chunk>",'
                    '"priority":50,'
                    '"success_criteria":["<verifiable result>"]'
                    "}}"
                    "\nOR\n"
                    "{"
                    '"tool":"finish_task_creation",'
                    '"arguments":{}'
                    "}"
                ),
            )

            tool_name = action["tool"]
            arguments = action["arguments"]

            if (
                tool_name
                == "finish_task_creation"
            ):
                if arguments:
                    self._remember_tool_error(
                        "finish_task_creation "
                        "takes no arguments"
                    )
                    continue

                if not workspace._tasks:
                    self._remember_tool_error(
                        "cannot finish task creation: "
                        "plan has no tasks"
                    )
                    continue

                return

            if tool_name != "create_task":
                self._remember_tool_error(
                    "TASKS phase allows only "
                    "create_task or "
                    "finish_task_creation"
                )
                continue

            result = self._execute_create_task(
                workspace,
                arguments,
            )

            if not result["ok"]:
                self._remember_tool_error(
                    result["error"]
                )

        if not workspace._tasks:
            raise ToolPlannerError(
                "task creation phase ended "
                "without tasks"
            )

        # Small models may forget the explicit finish action.
        # If they created valid tasks within the task budget,
        # proceed deterministically instead of looping forever.

    # ==========================================================
    # PHASE 2 — ONE CONTRACT AT A TIME
    # ==========================================================

    def _collect_contract(
        self,
        workspace: PlannerWorkspace,
        task_key: str,
        *,
        required_produce_hint: (
            str | None
        ) = None,
    ) -> None:
        for _ in range(
            self.max_contract_attempts
        ):
            state = workspace.inspect_plan()

            hint_text = ""

            if required_produce_hint:
                hint_text = (
                    "\n\nIMPORTANT REPAIR CONSTRAINT:\n"
                    "This task must produce the exact "
                    "internal resource:\n"
                    f"{required_produce_hint}"
                )

            action = self._ask_action(
                phase=(
                    f"CONTRACT:{task_key}"
                ),
                system_prompt=(
                    "You are in CONTRACT phase. "
                    "You may set the contract for exactly "
                    "one named task. "
                    "Return exactly one JSON object using "
                    "tool set_task_contract. "
                    "Do not create, update, remove, inspect, "
                    "validate, or finish anything. "
                    "requires = internal resources produced "
                    "by OTHER tasks in this plan. "
                    "produces = internal resources created "
                    "by this task. "
                    "external_dependencies = third-party "
                    "libraries, frameworks, packages, SDKs, "
                    "services, or external software. "
                    "Do not copy category names as actual "
                    "dependencies. "
                    "Never put the same resource in both "
                    "requires and produces."
                ),
                user_prompt=(
                    "USER REQUEST:\n"
                    f"{workspace.user_request}\n\n"
                    "CURRENT PLAN:\n"
                    + json.dumps(
                        state,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n\n"
                    "CURRENT TASK KEY:\n"
                    f"{task_key}"
                    f"{hint_text}\n\n"
                    "Return:\n"
                    "{"
                    '"tool":"set_task_contract",'
                    '"arguments":{'
                    f'"task_key":"{task_key}",'
                    '"requires":["<internal resource if needed>"],'
                    '"produces":["<internal resource produced>"],'
                    '"external_dependencies":["<actual external dependency if needed>"]'
                    "}}"
                ),
            )

            if (
                action["tool"]
                != "set_task_contract"
            ):
                self._remember_tool_error(
                    "CONTRACT phase allows only "
                    "set_task_contract"
                )
                continue

            arguments = action[
                "arguments"
            ]

            if (
                arguments.get(
                    "task_key"
                )
                != task_key
            ):
                self._remember_tool_error(
                    "contract task_key must be "
                    f"exactly '{task_key}'"
                )
                continue

            result = self._execute_contract(
                workspace,
                arguments,
            )

            if result["ok"]:
                return

            self._remember_tool_error(
                result["error"]
            )

        raise ToolPlannerError(
            "model could not provide a valid "
            f"contract for task '{task_key}'"
        )

    # ==========================================================
    # PHASE 3/4 — VALIDATE + TARGETED REPAIR
    # ==========================================================

    def _repair_one_error(
        self,
        workspace: PlannerWorkspace,
        error: str,
    ) -> None:
        missing = (
            self._NO_PRODUCER_RE.search(
                error
            )
        )

        if missing:
            target_task = missing.group(1)
            resource = missing.group(2)

            self._repair_missing_producer(
                workspace,
                target_task=target_task,
                resource=resource,
                validation_error=error,
            )

            return

        multiple = (
            self._MULTIPLE_PRODUCERS_RE.search(
                error
            )
        )

        if multiple:
            target_task = (
                multiple.group(1)
            )

            self._repair_existing_contract(
                workspace,
                target_task=target_task,
                validation_error=error,
            )

            return

        raise ToolPlannerError(
            "unsupported validation error "
            "for controlled repair: "
            f"{error}"
        )

    def _repair_missing_producer(
        self,
        workspace: PlannerWorkspace,
        *,
        target_task: str,
        resource: str,
        validation_error: str,
    ) -> None:
        for _ in range(
            self.max_contract_attempts
        ):
            state = workspace.inspect_plan()

            action = self._ask_action(
                phase="REPAIR:MISSING_PRODUCER",
                system_prompt=(
                    "You are in TARGETED REPAIR phase. "
                    "There is exactly one validation error. "
                    "You may do exactly one of two things: "
                    "1) create_task if the missing internal "
                    "resource really needs another "
                    "implementation task; "
                    "2) set_task_contract for the failing "
                    "task if that requirement was wrong or "
                    "should not be internal. "
                    "Do not delete tasks. "
                    "Do not finish the plan. "
                    "Do not repeat an action that already "
                    "failed."
                ),
                user_prompt=(
                    "USER REQUEST:\n"
                    f"{workspace.user_request}\n\n"
                    "VALIDATION ERROR:\n"
                    f"{validation_error}\n\n"
                    "FAILING TASK:\n"
                    f"{target_task}\n\n"
                    "MISSING RESOURCE:\n"
                    f"{resource}\n\n"
                    "CURRENT PLAN:\n"
                    + json.dumps(
                        state,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n\n"
                    "Return exactly one action: "
                    "create_task OR set_task_contract."
                ),
            )

            if (
                action["tool"]
                == "set_task_contract"
            ):
                arguments = action[
                    "arguments"
                ]

                if (
                    arguments.get(
                        "task_key"
                    )
                    != target_task
                ):
                    self._remember_tool_error(
                        "repair contract must target "
                        f"'{target_task}'"
                    )
                    continue

                result = self._execute_contract(
                    workspace,
                    arguments,
                )

                if result["ok"]:
                    return

                self._remember_tool_error(
                    result["error"]
                )
                continue

            if (
                action["tool"]
                == "create_task"
            ):
                result = self._execute_create_task(
                    workspace,
                    action["arguments"],
                )

                if not result["ok"]:
                    self._remember_tool_error(
                        result["error"]
                    )
                    continue

                new_key = str(
                    result["task_key"]
                )

                self._collect_contract(
                    workspace,
                    new_key,
                    required_produce_hint=(
                        resource
                    ),
                )

                return

            self._remember_tool_error(
                "repair phase allows only "
                "create_task or set_task_contract"
            )

        raise ToolPlannerError(
            "model could not repair missing "
            f"producer for resource '{resource}'"
        )

    def _repair_existing_contract(
        self,
        workspace: PlannerWorkspace,
        *,
        target_task: str,
        validation_error: str,
    ) -> None:
        for _ in range(
            self.max_contract_attempts
        ):
            state = workspace.inspect_plan()

            action = self._ask_action(
                phase="REPAIR:CONTRACT",
                system_prompt=(
                    "You are in TARGETED REPAIR phase. "
                    "Return exactly one "
                    "set_task_contract action for the "
                    "specified failing task. "
                    "Do not create or delete tasks."
                ),
                user_prompt=(
                    "VALIDATION ERROR:\n"
                    f"{validation_error}\n\n"
                    "FAILING TASK:\n"
                    f"{target_task}\n\n"
                    "CURRENT PLAN:\n"
                    + json.dumps(
                        state,
                        ensure_ascii=False,
                        indent=2,
                    )
                ),
            )

            if (
                action["tool"]
                != "set_task_contract"
            ):
                self._remember_tool_error(
                    "repair requires "
                    "set_task_contract"
                )
                continue

            arguments = action[
                "arguments"
            ]

            if (
                arguments.get(
                    "task_key"
                )
                != target_task
            ):
                self._remember_tool_error(
                    "repair contract must target "
                    f"'{target_task}'"
                )
                continue

            result = self._execute_contract(
                workspace,
                arguments,
            )

            if result["ok"]:
                return

            self._remember_tool_error(
                result["error"]
            )

        raise ToolPlannerError(
            "model could not repair "
            f"contract for '{target_task}'"
        )

    # ==========================================================
    # EXECUTION HELPERS
    # ==========================================================

    def _execute_create_task(
        self,
        workspace: PlannerWorkspace,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "key",
            "title",
            "description",
            "priority",
            "success_criteria",
        }

        required = {
            "key",
            "title",
            "description",
            "success_criteria",
        }

        return self._execute_checked(
            workspace.create_task,
            arguments,
            allowed=allowed,
            required=required,
        )

    def _execute_contract(
        self,
        workspace: PlannerWorkspace,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "task_key",
            "requires",
            "produces",
            "external_dependencies",
        }

        return self._execute_checked(
            workspace.set_task_contract,
            arguments,
            allowed=allowed,
            required=allowed,
        )

    @staticmethod
    def _execute_checked(
        function,
        arguments: dict[str, Any],
        *,
        allowed: set[str],
        required: set[str],
    ) -> dict[str, Any]:
        if not isinstance(
            arguments,
            dict,
        ):
            return {
                "ok": False,
                "error": (
                    "arguments must be "
                    "a JSON object"
                ),
            }

        keys = set(arguments)

        missing = required - keys
        unexpected = keys - allowed

        if missing:
            return {
                "ok": False,
                "error": (
                    "missing arguments: "
                    + ", ".join(
                        sorted(missing)
                    )
                ),
            }

        if unexpected:
            return {
                "ok": False,
                "error": (
                    "unexpected arguments: "
                    + ", ".join(
                        sorted(unexpected)
                    )
                ),
            }

        try:
            return function(
                **arguments
            )

        except (
            PlannerToolError,
            TypeError,
            ValueError,
        ) as error:
            return {
                "ok": False,
                "error": str(error),
            }

    # ==========================================================
    # MODEL I/O + ANTI-LOOP
    # ==========================================================

    def _ask_action(
        self,
        *,
        phase: str,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        last_error = getattr(
            self,
            "_last_tool_error",
            None,
        )

        if last_error:
            user_prompt += (
                "\n\nPREVIOUS ERROR:\n"
                f"{last_error}\n"
                "Correct that exact error. "
                "Do not repeat the same invalid action."
            )

            self._last_tool_error = None

        response = self.llm.chat(
            [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            max_new_tokens=384,
        )

        action = self._parse_action(
            response
        )

        signature = (
            phase
            + "|"
            + json.dumps(
                action,
                ensure_ascii=False,
                sort_keys=True,
            )
        )

        if (
            signature
            == self._same_action_signature
        ):
            self._same_action_count += 1
        else:
            self._same_action_signature = (
                signature
            )
            self._same_action_count = 1

        if (
            self._same_action_count
            > self.max_same_action
        ):
            raise ToolPlannerError(
                "anti-loop stopped repeated "
                f"identical action in phase {phase}: "
                f"{action['tool']}"
            )

        return action

    def _remember_tool_error(
        self,
        error: str,
    ) -> None:
        self._last_tool_error = str(
            error
        )

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


# Keep the public name used by PlannerRuntime and existing imports.
ToolPlanner = ControlledToolPlanner
