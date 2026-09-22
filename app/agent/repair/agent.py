from __future__ import annotations

import json
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


def build_debugger_messages(
    *,
    task_title: str,
    failure_class: str,
    reason: str,
    evidence: list[str],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are the Debugger Agent. Analyze only the supplied "
                "failure evidence. Do not reveal hidden chain-of-thought. "
                "Return one compact JSON object with string fields "
                "root_cause, do_not_repeat, and next_action. The next action "
                "must be materially different and stay within the current "
                "task scope."
            ),
        },
        {
            "role": "user",
            "content": (
                f"TASK: {task_title}\n"
                f"FAILURE_CLASS: {failure_class}\n"
                f"REASON: {reason}\n"
                "EVIDENCE:\n- " + "\n- ".join(evidence[:8])
            ),
        },
    ]


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
    evidence: list[str] = field(default_factory=list)
    do_not_repeat: list[str] = field(default_factory=list)
    analysis_effort: str = "LOW"
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

    @staticmethod
    def _analysis_effort(analysis: FailureAnalysis) -> str:
        if analysis.failure_class in {
            "REPEATED_OBSERVATION",
            "PROTOCOL_ERROR",
        }:
            return "LOW"
        if analysis.failure_class == "DEPENDENCY_BLOCKED":
            return "HIGH"
        return "MEDIUM"

    @staticmethod
    def _fallback_diagnosis(
        analysis: FailureAnalysis,
        strategy: str,
    ) -> tuple[str, str, list[str]]:
        if analysis.failure_class == "REPEATED_OBSERVATION":
            return (
                analysis.reason,
                "Stop repeating the observation; use the existing result and "
                "perform the requested edit. If the target is absent, create it.",
                ["the same observation action"],
            )
        return (
            analysis.reason,
            f"Use {strategy} with a materially different implementation approach.",
            ["the failed approach fingerprint"],
        )

    def _diagnose(
        self,
        *,
        task,
        analysis: FailureAnalysis,
        strategy: str,
    ) -> tuple[str, str, list[str], str]:
        effort = self._analysis_effort(analysis)
        root_cause, next_action, do_not_repeat = self._fallback_diagnosis(
            analysis,
            strategy,
        )
        if self.llm is None:
            return root_cause, next_action, do_not_repeat, effort

        token_budget = {"LOW": 160, "MEDIUM": 256, "HIGH": 384}[effort]
        messages = build_debugger_messages(
            task_title=str(getattr(task, "title", task)),
            failure_class=analysis.failure_class,
            reason=analysis.reason,
            evidence=analysis.evidence,
        )
        try:
            raw = self.llm.chat(messages, max_new_tokens=token_budget).strip()
            start = raw.find("{")
            if start < 0:
                raise ValueError("debugger did not return JSON")
            parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
            values = [parsed.get(key) for key in ("root_cause", "do_not_repeat", "next_action")]
            if not all(isinstance(value, str) and value.strip() for value in values):
                raise ValueError("debugger diagnosis fields are invalid")
            root_cause = str(values[0]).strip()[:800]
            do_not_repeat = [str(values[1]).strip()[:500]]
            next_action = str(values[2]).strip()[:800]
        except Exception:
            # Repair must remain available even when the diagnostic model fails.
            pass
        return root_cause, next_action, do_not_repeat, effort

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

        stable_root_cause = analysis.reason or self._root_cause(verification)
        fingerprint = approach_fingerprint(
            analysis.failure_class,
            stable_root_cause,
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

        root_cause, next_action, do_not_repeat, analysis_effort = self._diagnose(
            task=task,
            analysis=analysis,
            strategy=strategy,
        )

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
            new_approach=next_action,
            files_to_inspect=self._files_to_inspect(
                verification
            ),
            verification_plan=list(
                getattr(verification, "evidence", [])
                or []
            )[:5],
            evidence=list(analysis.evidence)[:8],
            do_not_repeat=do_not_repeat,
            analysis_effort=analysis_effort,
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

