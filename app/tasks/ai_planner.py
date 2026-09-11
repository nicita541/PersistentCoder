from __future__ import annotations

import json
from copy import deepcopy
from typing import Callable, Protocol, TypeVar

from app.tasks.dependencies import (
    DependencyValidationError,
    validate_task_graph,
)
from app.tasks.models import (
    PlanDraft,
    TaskDraft,
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


class AIPlannerError(
    RuntimeError
):
    pass


T = TypeVar("T")


class AIPlanner:
    MAX_TASKS = 10

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

    # ==========================================
    # PUBLIC API
    # ==========================================

    def plan(
        self,
        user_request: str,
    ) -> PlanDraft:
        user_request = (
            user_request.strip()
        )

        if not user_request:
            raise AIPlannerError(
                "user request is required"
            )

        goal = self._analyze_goal(
            user_request
        )

        tasks = self._decompose_tasks(
            user_request=user_request,
            goal=goal,
        )

        tasks = self._build_dependencies(
            user_request=user_request,
            goal=goal,
            tasks=tasks,
        )

        try:
            validate_task_graph(
                tasks
            )

        except DependencyValidationError as error:
            raise AIPlannerError(
                "Invalid task graph: "
                f"{error}"
            ) from error

        return PlanDraft(
            user_request=user_request,
            global_goal=(
                str(
                    goal["global_goal"]
                )
            ),
            tasks=tasks,
        )

    # ==========================================
    # GENERATE + REPAIR
    # ==========================================

    def _generate_with_repair(
            self,
            *,
            initial_messages: list[
                dict[str, str]
            ],
            parser: Callable[
                [str],
                T,
            ],
            max_new_tokens: int,
            stage_name: str,
            repair_context: str | None = None,
    ) -> T:
        messages = list(
            initial_messages
        )

        last_error: Exception | None = None

        for attempt_index in range(
                self.max_repair_attempts
                + 1
        ):
            response = self.llm.chat(
                messages,
                max_new_tokens=(
                    max_new_tokens
                ),
            )

            try:
                return parser(
                    response
                )

            except (
                    AIPlannerError,
                    DependencyValidationError,
            ) as error:
                last_error = error

                if (
                        attempt_index
                        >= self.max_repair_attempts
                ):
                    break

                messages = (
                    self._build_repair_messages(
                        stage_name=stage_name,
                        broken_response=response,
                        error=error,
                        repair_context=(
                            repair_context
                        ),
                    )
                )

        raise AIPlannerError(
            f"{stage_name} repair failed "
            f"after "
            f"{self.max_repair_attempts} "
            f"repair attempts: "
            f"{last_error}"
        ) from last_error

    @staticmethod
    def _build_repair_messages(
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

    # ==========================================
    # PASS 1 — GOAL ANALYZER
    # ==========================================

    def _analyze_goal(
        self,
        user_request: str,
    ) -> dict[str, object]:
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

        return self._generate_with_repair(
            initial_messages=messages,
            parser=self._parse_goal_response,
            max_new_tokens=384,
            stage_name="GOAL_ANALYZER",
            repair_context=(
                "USER REQUEST:\n"
                f"{user_request}"
            ),
        )

    def _parse_goal_response(
        self,
        response: str,
    ) -> dict[str, object]:
        data = self._parse_json_object(
            response
        )

        global_goal = self._require_string(
            data,
            "global_goal",
        )

        constraints = self._require_string_list(
            data,
            "constraints",
        )

        assumptions = self._require_string_list(
            data,
            "assumptions",
        )

        success_criteria = (
            self._require_string_list(
                data,
                "success_criteria",
            )
        )

        if not success_criteria:
            raise AIPlannerError(
                "Goal success_criteria "
                "cannot be empty"
            )

        return {
            "global_goal": global_goal,
            "constraints": constraints,
            "assumptions": assumptions,
            "success_criteria": (
                success_criteria
            ),
        }

    # ==========================================
    # PASS 2 — TASK DECOMPOSER
    # ==========================================

    def _decompose_tasks(
            self,
            *,
            user_request: str,
            goal: dict[str, object],
    ) -> list[TaskDraft]:
        goal_json = json.dumps(
            goal,
            ensure_ascii=False,
            indent=2,
        )

        messages = [
            {
                "role": "system",
                "content": (
                "You are the Task Decomposer "
                "inside a coding planner. "
                "Return JSON only. "
                "Create coarse implementation "
                "tasks, not tiny actions. "
                "Do not add dependencies yet. "
            
                "IMPORTANT CONTRACT RULES: "
            
                "requires means ONLY internal "
                "resources produced by another "
                "task in this plan. "
            
                "Libraries, frameworks, packages, "
                "SDKs and external software must "
                "go to external_dependencies. "
            
                "For example Flask is NOT a "
                "requires resource. "
                "Flask belongs in "
                "external_dependencies. "
            
                "Every required internal resource "
                "MUST be produced by another task. "
            
                "Use exactly the same resource "
                "name in requires and produces."
            ),
            },
            {
                "role": "user",
                "content": (
                    "USER REQUEST:\n"
                    f"{user_request}\n\n"
                    "GOAL ANALYSIS:\n"
                    f"{goal_json}\n\n"
                    "Create between 1 and 10 "
                    "coarse tasks.\n\n"
                    "RULES:\n"

                    "- every task key must be "
                    "unique\n"
                    
                    "- do not add depends_on yet\n"
                    
                    "- requires contains ONLY "
                    "outputs of other tasks\n"
                    
                    "- every internal item in "
                    "requires must be produced by "
                    "exactly one OTHER task\n"
                    
                    "- libraries, frameworks, "
                    "packages and SDKs go to "
                    "external_dependencies\n"
                    
                    "- Flask, FastAPI, pytest and "
                    "similar external software "
                    "must NOT be put in requires\n"
                    
                    "- requires and produces "
                    "resource names must match "
                    "exactly\n"
                    "- if a task requires storage "
                    "or a database, include a "
                    "task that produces that "
                    "resource\n\n"
                    "Return:\n"
                    "{\n"
                    '  "tasks": [\n'
                    "    {\n"
                    '      "key": "string",\n'
                    '      "title": "string",\n'
                    '      "description": '
                    '"string",\n'
                    '      "priority": 50,\n'
                    '      "requires": '
                    '["internal resource"],\n'
                    '      "external_dependencies": '
                    '["library or framework"],\n'
                    '      "produces": '
                    '["internal resource"],\n'
                    '      "success_criteria": '
                    '["criterion"]\n'
                    "    }\n"
                    "  ]\n"
                    "}"
                ),
            },
        ]

        def parse_tasks(
                response: str,
        ) -> list[TaskDraft]:
            tasks = (
                self._parse_tasks_response(
                    response
                )
            )

            self._validate_task_contracts(
                tasks
            )

            return tasks

        return self._generate_with_repair(
            initial_messages=messages,
            parser=parse_tasks,
            max_new_tokens=1024,
            stage_name="TASK_DECOMPOSER",
            repair_context=(
                "USER REQUEST:\n"
                f"{user_request}\n\n"

                "GOAL ANALYSIS:\n"
                f"{goal_json}\n\n"

                "AUTHORITATIVE RULES:\n"

                "- create 1 to 10 tasks\n"

                "- task keys must be unique\n"

                "- do not create depends_on "
                "here\n"

                "- requires contains ONLY "
                "internal resources produced "
                "by another task\n"

                "- every requires resource "
                "must be produced by exactly "
                "one other task\n"

                "- external libraries, "
                "frameworks, packages and "
                "SDKs belong in "
                "external_dependencies\n"

                "- Flask, FastAPI, pytest and "
                "similar software must NOT "
                "be put in requires\n"

                "- if validation says an "
                "external library has no "
                "producer, move it from "
                "requires to "
                "external_dependencies\n"

                "- requires and produces "
                "names must match exactly"
            ),
        )

    def _parse_tasks_response(
        self,
        response: str,
    ) -> list[TaskDraft]:
        data = self._parse_json_object(
            response
        )

        raw_tasks = data.get(
            "tasks"
        )

        if not isinstance(
            raw_tasks,
            list,
        ):
            raise AIPlannerError(
                "tasks must be a list"
            )

        if not (
            1
            <= len(raw_tasks)
            <= self.MAX_TASKS
        ):
            raise AIPlannerError(
                "task count must be 1..10"
            )

        result: list[
            TaskDraft
        ] = []

        for index, raw_task in enumerate(
            raw_tasks,
            start=1,
        ):
            if not isinstance(
                raw_task,
                dict,
            ):
                raise AIPlannerError(
                    "each task must be "
                    "a JSON object"
                )

            key = self._require_string(
                raw_task,
                "key",
            )

            title = self._require_string(
                raw_task,
                "title",
            )

            description = (
                self._require_string(
                    raw_task,
                    "description",
                )
            )

            priority = raw_task.get(
                "priority",
                50,
            )

            if (
                not isinstance(
                    priority,
                    int,
                )
                or isinstance(
                    priority,
                    bool,
                )
            ):
                raise AIPlannerError(
                    f"task {index} priority "
                    "must be integer"
                )

            priority = max(
                0,
                min(
                    100,
                    priority,
                ),
            )

            requires = (
                self._require_string_list(
                    raw_task,
                    "requires",
                )
            )

            external_dependencies = (
                self._optional_string_list(
                    raw_task,
                    "external_dependencies",
                )
            )

            produces = (
                self._require_string_list(
                    raw_task,
                    "produces",
                )
            )

            success_criteria = (
                self._require_string_list(
                    raw_task,
                    "success_criteria",
                )
            )

            if not success_criteria:
                raise AIPlannerError(
                    f"task '{key}' requires "
                    "success_criteria"
                )

            result.append(
                TaskDraft(
                    key=key,
                    title=title,
                    description=description,
                    priority=priority,
                    requires=requires,
                    external_dependencies=(
                        external_dependencies
                    ),
                    produces=produces,
                    success_criteria=(
                        success_criteria
                    ),
                )
            )

        return result


    def _validate_task_contracts(
        self,
        tasks: list[TaskDraft],
    ) -> None:
        """
        Проверяет Task graph ещё ДО
        Dependency Builder.

        Здесь пока нет depends_on.

        Мы проверяем контракт:

        requires
            ↓
        должен иметь producer
            ↓
        produces другого Task
        """

        task_keys: set[str] = set()

        producers: dict[
            str,
            list[str],
        ] = {}

        # --------------------------------------
        # TASK KEYS + PRODUCERS
        # --------------------------------------

        for task in tasks:
            if (
                task.key is None
                or not task.key.strip()
            ):
                raise AIPlannerError(
                    "Task contract invalid: "
                    "task key is required"
                )

            normalized_key = (
                task.key
                .strip()
                .casefold()
            )

            if (
                normalized_key
                in task_keys
            ):
                raise AIPlannerError(
                    "Task contract invalid: "
                    "duplicate task key "
                    f"'{task.key}'"
                )

            task_keys.add(
                normalized_key
            )

            for product in task.produces:
                normalized_product = (
                    self._normalize_resource(
                        product
                    )
                )

                if not normalized_product:
                    continue

                producers.setdefault(
                    normalized_product,
                    [],
                ).append(
                    task.key
                )

        # --------------------------------------
        # REQUIRES -> PRODUCES
        # --------------------------------------

        for task in tasks:
            assert task.key is not None

            for requirement in (
                task.requires
            ):
                normalized_requirement = (
                    self._normalize_resource(
                        requirement
                    )
                )

                producer_keys = [
                    producer_key
                    for producer_key
                    in producers.get(
                        normalized_requirement,
                        [],
                    )
                    if (
                        producer_key
                        .casefold()
                        != task.key.casefold()
                    )
                ]

                if not producer_keys:
                    raise AIPlannerError(
                        "Task contract invalid: "
                        f"task '{task.key}' "
                        "requires resource "
                        f"'{requirement}', "
                        "but it has no producer"
                    )

                if len(producer_keys) > 1:
                    raise AIPlannerError(
                        "Task contract invalid: "
                        f"task '{task.key}' "
                        "requires resource "
                        f"'{requirement}', "
                        "but it has multiple "
                        "producers: "
                        + ", ".join(
                            producer_keys
                        )
                    )


    # ==========================================
    # PASS 3 — DEPENDENCY BUILDER
    # ==========================================

    def _build_dependencies(
        self,
        *,
        user_request: str,
        goal: dict[str, object],
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        task_payload = [
            {
                "key": task.key,
                "title": task.title,
                "requires": task.requires,
                "external_dependencies": (
                    task.external_dependencies
                ),
                "produces": task.produces,
            }
            for task in tasks
        ]

        dependency_repair_context = (
            json.dumps(
                {
                    "allowed_task_keys": [
                        task.key
                        for task in tasks
                    ],
                    "tasks": task_payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Dependency "
                    "Builder inside a coding "
                    "planner. Return JSON only. "
                    "Build a DAG. "
                    "A task may depend only on "
                    "task keys that exist. "
                    "Do not create cycles. "
                    "external_dependencies NEVER create "
                    "task dependencies. Only requires "
                    "and produces determine dependencies."
                ),
            },
            {
                "role": "user",
                "content": (
                    "USER REQUEST:\n"
                    f"{user_request}\n\n"
                    "GOAL:\n"
                    + json.dumps(
                        goal,
                        ensure_ascii=False,
                    )
                    + "\n\nTASKS:\n"
                    + json.dumps(
                        task_payload,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n\n"
                    "Return dependencies for "
                    "EVERY task key:\n"
                    "{\n"
                    '  "dependencies": {\n'
                    '    "task_key": '
                    '["dependency_key"]\n'
                    "  }\n"
                    "}"
                ),
            },
        ]

        def parse_dependencies(
            response: str,
        ) -> list[TaskDraft]:
            result = (
                self._parse_dependencies_response(
                    response=response,
                    tasks=tasks,
                )
            )

            validate_task_graph(
                result
            )

            return result

        try:
            return self._generate_with_repair(
                initial_messages=messages,
                parser=parse_dependencies,
                max_new_tokens=640,
                stage_name=(
                    "DEPENDENCY_BUILDER"
                ),
                repair_context=(
                    dependency_repair_context
                ),
            )

        except AIPlannerError as model_error:
            # В strict mode ничего автоматически
            # не исправляем.
            #
            # Это сохраняет поведение старых
            # unit tests:
            #
            # AIPlanner(llm)
            # -> invalid graph -> ERROR.
            if self.max_repair_attempts == 0:
                raise AIPlannerError(
                    "Invalid task graph: "
                    f"{model_error}"
                ) from model_error

            # В runtime repair включён.
            #
            # Если даже после repair маленькая
            # модель продолжает ломать DAG,
            # строим dependencies программно
            # из requires / produces.
            try:
                return (
                    self
                    ._infer_dependencies_from_contracts(
                        tasks
                    )
                )

            except (
                    AIPlannerError,
                    DependencyValidationError,
            ) as fallback_error:
                raise AIPlannerError(
                    "Invalid task graph. "
                    "Model dependency builder "
                    "failed: "
                    f"{model_error}. "
                    "Deterministic dependency "
                    "fallback also failed: "
                    f"{fallback_error}"
                ) from fallback_error

    def _parse_dependencies_response(
        self,
        *,
        response: str,
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        data = self._parse_json_object(
            response
        )

        dependencies = data.get(
            "dependencies"
        )

        if not isinstance(
            dependencies,
            dict,
        ):
            raise AIPlannerError(
                "dependencies must be "
                "a JSON object"
            )

        expected_keys = {
            str(task.key)
            for task in tasks
        }

        returned_keys = {
            str(key)
            for key in dependencies
        }

        if (
            returned_keys
            != expected_keys
        ):
            raise AIPlannerError(
                "dependencies must contain "
                "every task exactly once"
            )

        result = deepcopy(
            tasks
        )

        for task in result:
            assert task.key is not None

            raw_dependencies = (
                dependencies[
                    task.key
                ]
            )

            if not isinstance(
                raw_dependencies,
                list,
            ):
                raise AIPlannerError(
                    "dependencies for "
                    f"'{task.key}' "
                    "must be a list"
                )

            parsed: list[str] = []

            for dependency in (
                raw_dependencies
            ):
                if (
                    not isinstance(
                        dependency,
                        str,
                    )
                    or not dependency.strip()
                ):
                    raise AIPlannerError(
                        "dependency keys "
                        "must be strings"
                    )

                parsed.append(
                    dependency.strip()
                )

            task.depends_on = parsed

        return result
    def _infer_dependencies_from_contracts(
        self,
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        """
        Deterministic fallback.

        Строит зависимости только из:

        Task.requires
        ↕
        Task.produces

        Никаких догадок модели здесь нет.
        """

        result = deepcopy(
            tasks
        )

        producers: dict[
            str,
            list[str],
        ] = {}

        # --------------------------------------
        # Сначала строим:
        #
        # resource -> task keys,
        # которые этот resource производят.
        # --------------------------------------

        for task in result:
            if task.key is None:
                raise AIPlannerError(
                    "task key is required "
                    "for dependency inference"
                )

            for product in task.produces:
                normalized_product = (
                    self._normalize_resource(
                        product
                    )
                )

                if not normalized_product:
                    continue

                producers.setdefault(
                    normalized_product,
                    [],
                ).append(
                    task.key
                )

        # --------------------------------------
        # Теперь для каждого requires ищем
        # Task, которая produces этот resource.
        # --------------------------------------

        for task in result:
            if task.key is None:
                raise AIPlannerError(
                    "task key is required "
                    "for dependency inference"
                )

            dependencies: list[str] = []

            for requirement in task.requires:
                normalized_requirement = (
                    self._normalize_resource(
                        requirement
                    )
                )

                matching_producers = [
                    producer_key
                    for producer_key
                    in producers.get(
                        normalized_requirement,
                        [],
                    )
                    if producer_key
                    != task.key
                ]

                if not matching_producers:
                    raise AIPlannerError(
                        "Cannot infer dependency "
                        f"for task '{task.key}': "
                        f"required resource "
                        f"'{requirement}' "
                        "has no producer"
                    )

                if len(
                    matching_producers
                ) > 1:
                    raise AIPlannerError(
                        "Cannot infer dependency "
                        f"for task '{task.key}': "
                        f"required resource "
                        f"'{requirement}' "
                        "has multiple producers: "
                        + ", ".join(
                            matching_producers
                        )
                    )

                dependency_key = (
                    matching_producers[0]
                )

                if (
                    dependency_key
                    not in dependencies
                ):
                    dependencies.append(
                        dependency_key
                    )

            task.depends_on = (
                dependencies
            )

        validate_task_graph(
            result
        )

        return result

    @staticmethod
    def _normalize_resource(
        value: str,
    ) -> str:
        return " ".join(
            value.casefold().split()
        )

    # ==========================================
    # JSON PARSER
    # ==========================================

    @staticmethod
    def _parse_json_object(
        text: str,
    ) -> dict[str, object]:
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

        raise AIPlannerError(
            "Model did not return "
            "a valid JSON object"
        )

    # ==========================================
    # SCHEMA HELPERS
    # ==========================================

    @staticmethod
    def _require_string(
        data: dict[str, object],
        key: str,
    ) -> str:
        value = data.get(
            key
        )

        if (
            not isinstance(
                value,
                str,
            )
            or not value.strip()
        ):
            raise AIPlannerError(
                f"'{key}' must be "
                "a non-empty string"
            )

        return value.strip()

    @staticmethod
    def _require_string_list(
        data: dict[str, object],
        key: str,
    ) -> list[str]:
        value = data.get(
            key
        )

        if not isinstance(
            value,
            list,
        ):
            raise AIPlannerError(
                f"'{key}' must be a list"
            )

        result: list[str] = []

        for item in value:
            if (
                not isinstance(
                    item,
                    str,
                )
                or not item.strip()
            ):
                raise AIPlannerError(
                    f"'{key}' must contain "
                    "only non-empty strings"
                )

            result.append(
                item.strip()
            )

        return result

    @staticmethod
    def _optional_string_list(
        data: dict[str, object],
        key: str,
    ) -> list[str]:
        value = data.get(
            key,
            [],
        )

        if not isinstance(
            value,
            list,
        ):
            raise AIPlannerError(
                f"'{key}' must be a list"
            )

        result: list[str] = []

        for item in value:
            if (
                not isinstance(
                    item,
                    str,
                )
                or not item.strip()
            ):
                raise AIPlannerError(
                    f"'{key}' must contain "
                    "only non-empty strings"
                )

            result.append(
                item.strip()
            )

        return result