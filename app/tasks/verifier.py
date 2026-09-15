from __future__ import annotations

from app.tasks.models import (
    StepStatus,
    TaskStatus,
    VerificationStatus,
)
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.verification_store import (
    VerificationStore,
)


class VerificationError(
    RuntimeError
):
    pass


class Verifier:
    def __init__(
        self,
        *,
        plan_store: PlanStore,
        step_store: StepStore,
        verification_store: VerificationStore,
    ) -> None:
        self.plan_store = plan_store
        self.step_store = step_store
        self.verification_store = (
            verification_store
        )

    @staticmethod
    def _require_evidence(
        evidence: list[str],
    ) -> None:
        cleaned = [
            item
            for item in evidence
            if item.strip()
        ]

        if not cleaned:
            raise VerificationError(
                "verification evidence "
                "is required"
            )

    # ==========================================
    # STEP VERIFICATION
    # ==========================================

    def begin_step_verification(
        self,
        step_id: int,
    ) -> None:
        step = self.step_store.get_step(
            step_id
        )

        if step is None:
            raise VerificationError(
                f"unknown step: {step_id}"
            )

        if (
            step.status
            is not StepStatus.IN_PROGRESS
        ):
            raise VerificationError(
                "step must be IN_PROGRESS "
                "before VERIFYING"
            )

        self.step_store.update_step_status(
            step_id,
            StepStatus.VERIFYING,
        )

    def pass_step(
        self,
        step_id: int,
        *,
        evidence: list[str],
    ) -> None:
        self._require_evidence(
            evidence
        )

        step = self.step_store.get_step(
            step_id
        )

        if step is None:
            raise VerificationError(
                f"unknown step: {step_id}"
            )

        if (
            step.status
            is not StepStatus.VERIFYING
        ):
            raise VerificationError(
                "step must be VERIFYING "
                "before PASS"
            )

        self.verification_store.record_step(
            step_id,
            status=VerificationStatus.PASS,
            evidence=evidence,
        )

        self.step_store.set_step_verification(
            step_id,
            verification_status="PASS",
            verification_evidence=evidence,
            failure_reason=None,
        )

        self.step_store.update_step_status(
            step_id,
            StepStatus.DONE,
        )

    def fail_step(
        self,
        step_id: int,
        *,
        reason: str,
        evidence: list[str],
    ) -> None:
        self._require_evidence(
            evidence
        )

        if not reason.strip():
            raise VerificationError(
                "verification failure "
                "reason is required"
            )

        step = self.step_store.get_step(
            step_id
        )

        if step is None:
            raise VerificationError(
                f"unknown step: {step_id}"
            )

        if (
            step.status
            is not StepStatus.VERIFYING
        ):
            raise VerificationError(
                "step must be VERIFYING "
                "before FAIL"
            )

        self.verification_store.record_step(
            step_id,
            status=VerificationStatus.FAIL,
            evidence=evidence,
            reason=reason,
        )

        self.step_store.set_step_verification(
            step_id,
            verification_status="FAIL",
            verification_evidence=evidence,
            failure_reason=reason,
        )

        self.step_store.update_step_status(
            step_id,
            StepStatus.IN_PROGRESS,
        )

    def block_step(
        self,
        step_id: int,
        *,
        reason: str,
        evidence: list[str],
    ) -> None:
        self._require_evidence(evidence)
        step = self.step_store.get_step(step_id)
        if step is None or step.status is not StepStatus.VERIFYING:
            raise VerificationError("step must be VERIFYING before BLOCKED")
        self.verification_store.record_step(
            step_id,
            status=VerificationStatus.BLOCKED,
            evidence=evidence,
            reason=reason,
        )
        self.step_store.set_step_verification(
            step_id,
            verification_status="BLOCKED",
            verification_evidence=evidence,
            failure_reason=reason,
        )
        self.step_store.update_step_status(step_id, StepStatus.BLOCKED)

    # ==========================================
    # TASK VERIFICATION
    # ==========================================

    def begin_task_verification(
        self,
        task_id: int,
    ) -> None:
        task = self.plan_store.get_task(
            task_id
        )

        if task is None:
            raise VerificationError(
                f"unknown task: {task_id}"
            )

        if (
            task.status
            is not TaskStatus.IN_PROGRESS
        ):
            raise VerificationError(
                "task must be IN_PROGRESS "
                "before VERIFYING"
            )

        steps = self.step_store.get_steps(
            task_id
        )

        if not steps:
            raise VerificationError(
                "task must have steps"
            )

        active_steps = [
            step
            for step in steps
            if step.status is not StepStatus.SUPERSEDED
        ]

        if not active_steps or not all(
            step.status is StepStatus.DONE
            for step in active_steps
        ):
            raise VerificationError(
                "all steps must be DONE "
                "before task verification"
            )

        self.plan_store.update_task_status(
            task_id,
            TaskStatus.VERIFYING,
        )

    def pass_task(
        self,
        task_id: int,
        *,
        evidence: list[str],
    ) -> None:
        self._require_evidence(
            evidence
        )

        task = self.plan_store.get_task(
            task_id
        )

        if task is None:
            raise VerificationError(
                f"unknown task: {task_id}"
            )

        if (
            task.status
            is not TaskStatus.VERIFYING
        ):
            raise VerificationError(
                "task must be VERIFYING "
                "before PASS"
            )

        self.verification_store.record_task(
            task_id,
            status=VerificationStatus.PASS,
            evidence=evidence,
        )

        self.plan_store.set_task_verification(
            task_id,
            verification_status="PASS",
            verification_evidence=evidence,
        )

        self.plan_store.update_task_status(
            task_id,
            TaskStatus.DONE,
        )

    def fail_task(
        self,
        task_id: int,
        *,
        reason: str,
        evidence: list[str],
    ) -> None:
        self._require_evidence(
            evidence
        )

        if not reason.strip():
            raise VerificationError(
                "verification failure "
                "reason is required"
            )

        task = self.plan_store.get_task(
            task_id
        )

        if task is None:
            raise VerificationError(
                f"unknown task: {task_id}"
            )

        if (
            task.status
            is not TaskStatus.VERIFYING
        ):
            raise VerificationError(
                "task must be VERIFYING "
                "before FAIL"
            )

        self.verification_store.record_task(
            task_id,
            status=VerificationStatus.FAIL,
            evidence=evidence,
            reason=reason,
        )

        self.plan_store.set_task_verification(
            task_id,
            verification_status="FAIL",
            verification_evidence=evidence,
        )

        self.plan_store.update_task_status(
            task_id,
            TaskStatus.IN_PROGRESS,
        )

    def block_task(
        self,
        task_id: int,
        *,
        reason: str,
        evidence: list[str],
    ) -> None:
        self._require_evidence(evidence)
        task = self.plan_store.get_task(task_id)
        if task is None or task.status is not TaskStatus.VERIFYING:
            raise VerificationError("task must be VERIFYING before BLOCKED")
        self.verification_store.record_task(
            task_id,
            status=VerificationStatus.BLOCKED,
            evidence=evidence,
            reason=reason,
        )
        self.plan_store.set_task_verification(
            task_id,
            verification_status="BLOCKED",
            verification_evidence=evidence,
        )
        self.plan_store.update_task_status(task_id, TaskStatus.BLOCKED)
