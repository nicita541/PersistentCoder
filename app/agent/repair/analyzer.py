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

