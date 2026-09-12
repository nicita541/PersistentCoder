from __future__ import annotations

from dataclasses import dataclass

from app.agent.repair.analyzer import (
    FailureAnalysis,
)
from app.agent.repair.strategies import (
    GIVE_UP,
    REPLAN_TASK,
    RETRY_STEP,
)


@dataclass(frozen=True)
class RepairOutcome:
    action: str
    scope: str
    strategy: str
    reason: str
    decision: object | None = None


class RepairAgent:
    """
    Управляющий агент восстановления.

    Использует существующие:
      - Replanner (app.tasks.replanner);
      - AttemptStore;
      - ReplanStore.

    Не перепланирует весь проект при локальной ошибке.
    """

    def __init__(
        self,
        *,
        analyzer,
        strategies,
        replanner,
        attempt_store=None,
        replan_store=None,
        llm=None,
    ) -> None:
        self.analyzer = analyzer
        self.strategies = strategies
        self.replanner = replanner
        self.attempt_store = attempt_store
        self.replan_store = replan_store
        self.llm = llm

    def analyze(
        self,
        *,
        task,
        verification,
    ) -> FailureAnalysis:
        return self.analyzer.analyze(
            task=task,
            verification=verification,
        )

    def repair(
        self,
        *,
        task,
        verification,
        step=None,
        step_attempt_number: int = 1,
        task_attempt_number: int = 1,
    ) -> RepairOutcome:
        analysis = self.analyzer.analyze(
            task=task,
            verification=verification,
        )

        strategy = self.strategies.select(
            analysis=analysis,
            step_attempts=step_attempt_number,
            task_attempts=task_attempt_number,
        )

        decision = self._decide(
            task=task,
            step=step,
            strategy=strategy,
            reason=analysis.reason,
        )

        return RepairOutcome(
            action=strategy,
            scope=decision.scope.value,
            strategy=strategy,
            reason=analysis.reason,
            decision=decision,
        )

    def _decide(
        self,
        *,
        task,
        step,
        strategy: str,
        reason: str,
    ):
        if (
            strategy == RETRY_STEP
            and step is not None
        ):
            return self.replanner.decide_step_failure(
                step.id,
                reason=reason,
            )

        if strategy == GIVE_UP:
            return self.replanner.decide_task_failure(
                task.id,
                reason=reason,
            )

        if (
            strategy == REPLAN_TASK
            or strategy == RETRY_STEP
        ):
            return self.replanner.decide_task_failure(
                task.id,
                reason=reason,
            )

        return self.replanner.decide_task_failure(
            task.id,
            reason=reason,
        )

    def suggest_approach(
        self,
        *,
        task,
        reason: str,
    ) -> str | None:
        """
        LLM нужна для предложения альтернативного подхода
        при исчерпании текущего.
        """

        if self.llm is None:
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Repair Agent. "
                    "Reply with a short alternative "
                    "implementation approach. "
                    "No code, no JSON."
                ),
            },
            {
                "role": "user",
                "content": (
                    "TASK:\n"
                    f"{getattr(task, 'title', task)}\n\n"
                    "FAILURE:\n"
                    f"{reason}"
                ),
            },
        ]

        answer = self.llm.chat(
            messages,
            max_new_tokens=256,
        ).strip()

        return answer or None

