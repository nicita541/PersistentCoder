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
        use_llm_dependencies: bool = True,
        goal_analyzer=None,
        decomposer=None,
        task_builder=None,
        contract_builder=None,
        dependency_builder=None,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError(
                "max_repair_attempts "
                "cannot be negative"
            )

        self.llm = llm
        self.task_store = task_store
        self.step_store = step_store
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

        components = self.decomposer.decompose(
            user_request=request,
            goal=goal,
        )

        tasks = self.task_builder.build(
            components
        )

        self.contract_builder.validate_contracts(
            tasks
        )

        if self.use_llm_dependencies:
            tasks = self.dependency_builder.build(
                user_request=request,
                goal=goal,
                tasks=tasks,
            )

        else:
            tasks = (
                self.dependency_builder
                .infer_from_contracts(tasks)
            )

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

            criteria = (
                list(task.success_criteria)
                or [task.title]
            )

            self.step_store.create_steps(
                task.id,
                [
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
                ],
            )

