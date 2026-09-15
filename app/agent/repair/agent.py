from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.repair.analyzer import (
    FailureAnalysis,
)
from app.agent.repair.strategies import (
    GIVE_UP,
    REPLAN_TASK,
    RETRY_STEP,
)
from app.agent.repair.context import approach_fingerprint


@dataclass(frozen=True)
class ApproachPlan:
    """
    Structured repair decision (not just free text).

    scope / failure_class / root_cause / new_approach /
    files_to_inspect / verification_plan
    """

    scope: str
    failure_class: str
    root_cause: str
    new_approach: str
    files_to_inspect: list[str] = field(
        default_factory=list
    )
    verification_plan: list[str] = field(
        default_factory=list
    )
    fingerprint: str = ""


@dataclass(frozen=True)
class RepairOutcome:
    action: str
    scope: str
    strategy: str
    reason: str
    decision: object | None = None
    approach: ApproachPlan | None = None


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
        system_prompt: str | None = None,
    ) -> None:
        self.analyzer = analyzer
        self.strategies = strategies
        self.replanner = replanner
        self.attempt_store = attempt_store
        self.replan_store = replan_store
        self.llm = llm
        self.system_prompt = system_prompt

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

    @staticmethod
    def _root_cause(verification) -> str:
        results = (
            getattr(
                verification,
                "criterion_results",
                [],
            )
            or []
        )

        for item in results:
            if (
                item.status in ("FAIL", "BLOCKED")
                and item.reason
            ):
                return item.reason.strip()

        return (
            getattr(verification, "reason", "")
            or "verification failed"
        ).strip()

    @staticmethod
    def _files_to_inspect(verification) -> list[str]:
        files: list[str] = []

        for item in (
            getattr(
                verification,
                "criterion_results",
                [],
            )
            or []
        ):
            criterion = getattr(item, "criterion", "")

            if criterion and criterion not in files:
                files.append(criterion.strip())

        return files[:5]

    def _was_attempted(
        self,
        step,
        approach: str,
    ) -> bool:
        """
        Was this exact approach already tried and FAILED/BLOCKED?
        """

        if self.attempt_store is None or step is None:
            return False

        try:
            records = (
                self.attempt_store
                .get_step_attempts(step.id)
            )

        except Exception:
            return False

        normalized = approach.casefold().strip()

        for record in records:
            if not record.approach:
                continue

            if (
                record.approach.casefold() == normalized
                and record.status.value
                in ("FAILED", "BLOCKED")
            ):
                return True

        return False

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

        root_cause = self._root_cause(verification)

        approach_text = (
            f"{analysis.failure_class}::{root_cause}"
        )
        fingerprint = approach_fingerprint(
            analysis.failure_class,
            root_cause,
        )

        strategy = self.strategies.select(
            analysis=analysis,
            step_attempts=step_attempt_number,
            task_attempts=task_attempt_number,
        )

        # --------------------------------------
        # NO REPEATING A FAILED APPROACH
        # --------------------------------------

        repeated = self._was_attempted(
            step,
            fingerprint,
        )

        if repeated:
            if strategy == RETRY_STEP:
                strategy = REPLAN_TASK

            elif strategy == REPLAN_TASK:
                strategy = GIVE_UP

        # Replanner-level approach guard.
        if step is not None and strategy == RETRY_STEP:
            try:
                self.replanner.assert_step_approach_allowed(
                    step.id,
                    fingerprint,
                )

            except Exception:
                strategy = REPLAN_TASK

        decision = self._decide(
            task=task,
            step=step,
            strategy=strategy,
            reason=analysis.reason,
        )

        scope = (
            decision.scope.value
            if decision is not None
            else analysis.scope
        )

        approach = ApproachPlan(
            scope=scope,
            failure_class=analysis.failure_class,
            root_cause=root_cause,
            new_approach=f"{strategy}::{approach_text}",
            files_to_inspect=self._files_to_inspect(
                verification
            ),
            verification_plan=list(
                getattr(verification, "evidence", [])
                or []
            )[:5],
            fingerprint=fingerprint,
        )

        return RepairOutcome(
            action=strategy,
            scope=scope,
            strategy=strategy,
            reason=analysis.reason,
            decision=decision,
            approach=approach,
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

        guard = (
            (self.system_prompt + "\n\n")
            if self.system_prompt
            else ""
        )

        messages = [
            {
                "role": "system",
                "content": (
                    guard
                    + "You are the Repair Agent. "
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

