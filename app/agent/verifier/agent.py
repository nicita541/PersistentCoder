from __future__ import annotations

from app.agent.state import (
    ExecutionResult,
    VerificationResult,
)
from app.agent.verifier.evidence import (
    EvidenceCollector,
)
from app.tasks.models import TaskStatus


class VerificationAgent:
    """
    Управляющий агент верификации.

    Использует:
      - низкоуровневый Verifier (app.tasks.verifier);
      - VerificationStore;
      - StepStore;
      - success criteria Task;
      - реальные результаты/evidence.

    Никакого фиктивного verified=True.
    """

    def __init__(
        self,
        *,
        quality_gate,
        verifier,
        plan_store,
        step_store,
        verification_store,
        evidence_collector=None,
    ) -> None:
        self.quality_gate = quality_gate
        self.verifier = verifier
        self.plan_store = plan_store
        self.step_store = step_store
        self.verification_store = (
            verification_store
        )
        self.evidence = (
            evidence_collector
            or EvidenceCollector()
        )

    def _collect_evidence(
        self,
        execution: ExecutionResult,
        result: VerificationResult,
    ) -> list[str]:
        evidence = self.evidence.collect(
            execution
        )

        if evidence:
            return evidence

        if result.evidence:
            return list(result.evidence)

        return ["execution_failed"]

    def verify_step(
        self,
        *,
        task,
        step,
        execution: ExecutionResult,
    ) -> VerificationResult:
        result = self.quality_gate.check(
            task=task,
            execution=execution,
        )

        evidence = self._collect_evidence(
            execution,
            result,
        )

        self.verifier.begin_step_verification(
            step.id
        )

        if result.ok:
            self.verifier.pass_step(
                step.id,
                evidence=evidence,
            )

        else:
            self.verifier.fail_step(
                step.id,
                reason=(
                    result.reason
                    or "execution failed"
                ),
                evidence=evidence,
            )

        return result

    def verify_task(
        self,
        *,
        task,
        execution: ExecutionResult,
    ) -> VerificationResult:
        result = self.quality_gate.check(
            task=task,
            execution=execution,
        )

        evidence = self._collect_evidence(
            execution,
            result,
        )

        self._begin_task_verification(task)

        if result.ok:
            self.verifier.pass_task(
                task.id,
                evidence=evidence,
            )

        else:
            self.verifier.fail_task(
                task.id,
                reason=(
                    result.reason
                    or "execution failed"
                ),
                evidence=evidence,
            )

        return result

    def _begin_task_verification(
        self,
        task,
    ) -> None:
        steps = self.step_store.get_steps(
            task.id
        )

        if steps:
            self.verifier.begin_task_verification(
                task.id
            )

        else:
            # Task без steps: переводим состояние
            # вручную, парную запись делает
            # низкоуровневый verifier ниже.
            self.plan_store.update_task_status(
                task.id,
                TaskStatus.VERIFYING,
            )

