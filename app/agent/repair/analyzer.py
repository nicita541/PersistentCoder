from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.state import VerificationResult
from app.tasks.models import ReplanScope


@dataclass(frozen=True)
class FailureAnalysis:
    failure_class: str
    scope: str
    reason: str
    evidence: list[str] = field(
        default_factory=list
    )


class FailureAnalyzer:
    """
    Классифицирует провал и выбирает МИНИМАЛЬНЫЙ scope.

    BLOCKED (проблема зависимости) -> TASK,
    обычный провал -> STEP.

    Полный replan проекта здесь невозможен:
    это защита от перепланирования всего
    проекта при локальной ошибке.
    """

    def analyze(
        self,
        *,
        task,
        verification: VerificationResult,
    ) -> FailureAnalysis:
        reason = (
            verification.reason or ""
        ).strip() or "verification failed"

        evidence = list(
            verification.evidence
        )

        repeated = next(
            (
                item
                for item in evidence
                if item.startswith("repeated_observation:")
            ),
            None,
        )
        if repeated is not None:
            parts = repeated.split(":", 3)
            action = parts[1] if len(parts) > 1 else "observation"
            target = parts[2] if len(parts) > 2 else "the same target"
            count = parts[3] if len(parts) > 3 else "multiple"
            return FailureAnalysis(
                failure_class="REPEATED_OBSERVATION",
                scope=ReplanScope.STEP.value,
                reason=(
                    f"{action} for {target} was repeated {count} times "
                    "without producing progress"
                ),
                evidence=evidence,
            )

        if verification.status == "BLOCKED":
            return FailureAnalysis(
                failure_class="DEPENDENCY_BLOCKED",
                scope=ReplanScope.TASK.value,
                reason=reason,
                evidence=evidence,
            )

        if not verification.ok:
            return FailureAnalysis(
                failure_class="VERIFICATION_FAIL",
                scope=ReplanScope.STEP.value,
                reason=reason,
                evidence=evidence,
            )

        return FailureAnalysis(
            failure_class="UNKNOWN",
            scope=ReplanScope.STEP.value,
            reason=reason,
            evidence=evidence,
        )

