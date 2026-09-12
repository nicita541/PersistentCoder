from __future__ import annotations

from app.agent.state import ExecutionResult


class EvidenceCollector:
    """
    Собирает реальные evidence из ExecutionResult.

    Верификатор не может обойтись без evidence —
    пустой список означает BLOCKED.
    """

    def collect(
        self,
        execution: ExecutionResult,
    ) -> list[str]:
        evidence: list[str] = []

        for item in execution.evidence:
            if (
                isinstance(item, str)
                and item.strip()
            ):
                evidence.append(item.strip())

        for command in execution.commands:
            text = command.as_evidence()

            if text not in evidence:
                evidence.append(text)

        for artifact in execution.artifacts:
            text = f"artifact: {artifact}"

            if text not in evidence:
                evidence.append(text)

        return evidence

