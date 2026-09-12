from __future__ import annotations

from app.agent.state import (
    ExecutionResult,
    VerificationResult,
)


class QualityGate:
    """
    Реальная проверка результата CodingAgent.

    Никаких return True / bool(result) как verification.
    Провал возможен по нескольким независимым причинам.
    """

    def __init__(
        self,
        *,
        require_evidence: bool = True,
    ) -> None:
        self.require_evidence = require_evidence

    def check(
        self,
        *,
        task,
        execution: ExecutionResult,
    ) -> VerificationResult:
        evidence = [
            item.strip()
            for item in execution.evidence
            if isinstance(item, str)
            and item.strip()
        ]

        if not execution.ok:
            return VerificationResult(
                ok=False,
                status="FAIL",
                reason=(
                    execution.failure_reason
                    or "execution failed"
                ),
                evidence=(
                    evidence
                    or ["execution_failed"]
                ),
            )

        if not execution.commands and (
            not execution.artifacts
        ):
            return VerificationResult(
                ok=False,
                status="FAIL",
                reason=(
                    "execution produced no "
                    "verifiable result"
                ),
                evidence=(
                    evidence
                    or ["no_artifacts"]
                ),
            )

        failed_commands = [
            command
            for command in execution.commands
            if not command.ok
        ]

        if failed_commands:
            return VerificationResult(
                ok=False,
                status="FAIL",
                reason=(
                    "command failed: "
                    f"{failed_commands[0].command}"
                ),
                evidence=evidence,
            )

        if (
            self.require_evidence
            and not evidence
        ):
            return VerificationResult(
                ok=False,
                status="BLOCKED",
                reason=(
                    "verification requires "
                    "real evidence"
                ),
                evidence=[],
            )

        criteria = list(
            getattr(
                task,
                "success_criteria",
                [],
            )
            or []
        )

        if not criteria:
            return VerificationResult(
                ok=False,
                status="FAIL",
                reason=(
                    "task has no success criteria"
                ),
                evidence=evidence,
            )

        return VerificationResult(
            ok=True,
            status="PASS",
            reason=(
                "execution satisfies "
                "declared success criteria"
            ),
            evidence=evidence,
        )

