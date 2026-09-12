from __future__ import annotations

from dataclasses import dataclass

from app.agent.planner.contract_builder import (
    ContractBuilder,
)
from app.agent.planner.decomposer import (
    GoalAnalyzer,
    PlannerError,
    PlannerLLM,
    TaskDecomposer,
)
from app.agent.planner.dependency_builder import (
    DependencyBuilder,
)
from app.agent.planner.step_validator import (
    StepValidator,
)
from app.agent.planner.task_builder import (
    TaskBuilder,
)
from app.tasks.models import (
    PlanDraft,
    StepDraft,
)


@dataclass(frozen=True)
class PlanningResult:
    plan_id: int
    draft: PlanDraft


class PlannerAgent:
    """
    Управляющий агент планирования.

    Поток:

        User Goal
            -> LLM semantic decision (GoalAnalyzer / TaskDecomposer)
            -> TaskBuilder (Python создаёт Task/Step/Contracts/IDs)
            -> DependencyBuilder
            -> Task OS (PlanStore)

    LLM НЕ меняет Task OS напрямую. Это делает Python.
    """

    def __init__(
        self,
        llm: PlannerLLM,
        task_store,
        *,
        step_store=None,
        max_repair_attempts: int = 0,
        max_plan_repairs: int = 0,
        use_llm_dependencies: bool = True,
        goal_analyzer=None,
        decomposer=None,
        task_builder=None,
        contract_builder=None,
        dependency_builder=None,
        step_validator=None,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError(
                "max_repair_attempts "
                "cannot be negative"
            )

        if max_plan_repairs < 0:
            raise ValueError(
                "max_plan_repairs "
                "cannot be negative"
            )

        self.llm = llm
        self.task_store = task_store
        self.step_store = step_store
        self.max_plan_repairs = max_plan_repairs
        self.use_llm_dependencies = (
            use_llm_dependencies
        )

        self.goal_analyzer = (
            goal_analyzer
            or GoalAnalyzer(
                llm,
                max_repair_attempts=(
                    max_repair_attempts
                ),
            )
        )

        self.decomposer = (
            decomposer
            or TaskDecomposer(
                llm,
                max_repair_attempts=(
                    max_repair_attempts
                ),
            )
        )

        self.task_builder = (
            task_builder or TaskBuilder()
        )
        self._step_drafts_by_key: dict = {}

        self.step_validator = (
            step_validator or StepValidator()
        )

        # Non-fatal step quality findings of the last planning pass
        # (reported to the user, never auto-passed).
        self.last_step_warnings: list[str] = []

        self.contract_builder = (
            contract_builder or ContractBuilder()
        )

        self.dependency_builder = (
            dependency_builder
            or DependencyBuilder(
                llm,
                max_repair_attempts=(
                    max_repair_attempts
                ),
            )
        )

    def build_draft(
        self,
        request: str,
    ) -> PlanDraft:
        request = request.strip()

        if not request:
            raise PlannerError(
                "user request is required"
            )

        goal = self.goal_analyzer.analyze(
            request
        )

        # --------------------------------------
        # PLAN-LEVEL BOUNDED REPAIR
        #
        # GoalAnalyzer -> TaskDecomposer -> TaskBuilder
        # -> ContractBuilder -> DependencyBuilder
        #
        # If the contract is invalid (for example two tasks
        # produce the same file path), the decomposition is
        # repaired and validation is repeated.
        # ContractBuilder itself is NOT weakened.
        # --------------------------------------

        repair_instruction: str | None = None

        previous_components: (
            list[dict[str, object]] | None
        ) = None

        tasks = None

        for attempt in range(
            self.max_plan_repairs + 1
        ):
            components = self.decomposer.decompose(
                user_request=request,
                goal=goal,
                repair_instruction=(
                    repair_instruction
                ),
                previous_components=(
                    previous_components
                ),
            )

            try:
                tasks = self.task_builder.build(
                    components
                )

                key_edges = (
                    self.task_builder.key_require_edges(
                        tasks
                    )
                )

                tasks = (
                    self.task_builder
                    .normalize_key_requires(tasks)
                )

                self._step_drafts_by_key = (
                    self.task_builder.build_steps(
                        components
                    )
                )

                step_errors = (
                    self.step_validator.validate_all(
                        tasks,
                        self._step_drafts_by_key,
                    )
                )

                if step_errors:
                    raise PlannerError(
                        "invalid step decomposition: "
                        + "; ".join(step_errors)
                    )

                self.last_step_warnings = (
                    self.step_validator.warnings_all(
                        tasks,
                        self._step_drafts_by_key,
                    )
                )

                self.contract_builder.validate_contracts(
                    tasks
                )

                if self.use_llm_dependencies:
                    tasks = (
                        self.dependency_builder
                        .build(
                            user_request=request,
                            goal=goal,
                            tasks=tasks,
                        )
                    )

                else:
                    tasks = (
                        self.dependency_builder
                        .infer_from_contracts(tasks)
                    )

                tasks = (
                    self.task_builder
                    .merge_key_require_edges(
                        tasks,
                        key_edges,
                    )
                )

            except PlannerError as error:
                if attempt >= self.max_plan_repairs:
                    if self.max_plan_repairs == 0:
                        raise

                    raise PlannerError(
                        "plan repair failed after "
                        f"{self.max_plan_repairs} "
                        f"attempts: {error}"
                    ) from error

                repair_instruction = str(error)
                previous_components = components
                continue

            break

        return PlanDraft(
            user_request=request,
            global_goal=str(
                goal["global_goal"]
            ),
            tasks=tasks,
        )

    def plan(
        self,
        request: str,
    ) -> PlanningResult:
        draft = self.build_draft(request)

        plan_id = self.task_store.create_plan(
            draft
        )

        self._create_steps(plan_id)

        return PlanningResult(
            plan_id=plan_id,
            draft=draft,
        )

    def replan(
        self,
        request: str,
        *,
        previous_plan_id: int,
        reason: str,
        invalidate_task_keys: tuple[str, ...] = (),
    ) -> PlanningResult:
        """
        GLOBAL PLAN REPLAN (level 3).

        Creates a NEW plan revision: the previous plan and its tasks
        stay in SQLite (marked SUPERSEDED), already DONE tasks are
        carried over, explicitly invalidated tasks are marked
        SUPERSEDED, and the WHY is stored with the new revision.
        """

        draft = self.build_draft(request)

        plan_id = self.task_store.create_replan(
            previous_plan_id,
            draft,
            reason=reason,
            invalidate_task_keys=tuple(
                invalidate_task_keys
            ),
        )

        self._create_steps(plan_id)

        return PlanningResult(
            plan_id=plan_id,
            draft=draft,
        )

    def _create_steps(
        self,
        plan_id: int,
    ) -> None:
        if self.step_store is None:
            return

        for task in self.task_store.get_tasks(
            plan_id
        ):
            if self.step_store.get_steps(task.id):
                continue

            planned_steps = (
                self._step_drafts_by_key.get(
                    task.key or "",
                    [],
                )
            )

            if planned_steps:
                step_drafts = list(planned_steps)

            else:
                criteria = (
                    list(task.success_criteria)
                    or [task.title]
                )

                step_drafts = [
                    StepDraft(
                        title=task.title,
                        description=(
                            task.description
                            or task.title
                        ),
                        requires=list(task.requires),
                        produces=list(task.produces),
                        success_criteria=criteria,
                    )
                ]

            self.step_store.create_steps(
                task.id,
                step_drafts,
            )

