from __future__ import annotations

from app.tasks.models import (
    StepStatus,
    TaskStatus,
    VerificationStatus,
    VerificationTargetType,
)
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.verification_store import (
    VerificationStore,
)
from app.tasks.verification_context import VerificationContext
from app.tasks.unit_of_work import RuntimeUnitOfWork


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
        state_authority=None,
    ) -> None:
        self.plan_store = plan_store
        self.step_store = step_store
        self.verification_store = (
            verification_store
        )
        store_paths = {
            store.database_path.resolve()
            for store in (plan_store, step_store, verification_store)
        }
        store_contexts = {
            store.context
            for store in (plan_store, step_store, verification_store)
        }
        if len(store_paths) != 1 or len(store_contexts) != 1:
            raise VerificationError(
                "verification stores must share one database and scope"
            )
        authority = state_authority or RuntimeUnitOfWork(
            verification_store.context or verification_store.database_path
        )
        authority_path = getattr(authority, "database_path", None)
        authority_context = getattr(authority, "context", None)
        if (
            authority_path is None
            or authority_path.resolve() != next(iter(store_paths))
            or authority_context != next(iter(store_contexts))
        ):
            raise VerificationError(
                "verification authority must share the store database and scope"
            )
        self.state_authority = authority

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

    @staticmethod
    def _require_trusted_context(
        context: VerificationContext | None,
    ) -> VerificationContext:
        if context is None:
            raise VerificationError(
                "PASS requires revision-bound verification context"
            )
        return context

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
        context: VerificationContext | None = None,
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

        context = self._require_trusted_context(context)

        self.state_authority.finish_verification(
                target_type=VerificationTargetType.STEP,
                target_id=step_id,
                verification_status="PASS",
                evidence=evidence,
                reason=None,
                context=context,
                target_status=StepStatus.DONE.value,
                failure_reason=None,
        )

    def fail_step(
        self,
        step_id: int,
        *,
        reason: str,
        evidence: list[str],
        context: VerificationContext | None = None,
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

        self.state_authority.finish_verification(
                target_type=VerificationTargetType.STEP,
                target_id=step_id,
                verification_status="FAIL",
                evidence=evidence,
                reason=reason,
                context=context,
                target_status=StepStatus.IN_PROGRESS.value,
                failure_reason=reason,
        )

    def block_step(
        self,
        step_id: int,
        *,
        reason: str,
        evidence: list[str],
        context: VerificationContext | None = None,
    ) -> None:
        self._require_evidence(evidence)
        step = self.step_store.get_step(step_id)
        if step is None or step.status is not StepStatus.VERIFYING:
            raise VerificationError("step must be VERIFYING before BLOCKED")
        self.state_authority.finish_verification(
                target_type=VerificationTargetType.STEP,
                target_id=step_id,
                verification_status="BLOCKED",
                evidence=evidence,
                reason=reason,
                context=context,
                target_status=StepStatus.BLOCKED.value,
                failure_reason=reason,
        )

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
        context: VerificationContext | None = None,
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

        context = self._require_trusted_context(context)

        self.state_authority.finish_verification(
                target_type=VerificationTargetType.TASK,
                target_id=task_id,
                verification_status="PASS",
                evidence=evidence,
                reason=None,
                context=context,
                target_status=TaskStatus.DONE.value,
        )

    def fail_task(
        self,
        task_id: int,
        *,
        reason: str,
        evidence: list[str],
        context: VerificationContext | None = None,
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

        self.state_authority.finish_verification(
                target_type=VerificationTargetType.TASK,
                target_id=task_id,
                verification_status="FAIL",
                evidence=evidence,
                reason=reason,
                context=context,
                target_status=TaskStatus.IN_PROGRESS.value,
        )

    def block_task(
        self,
        task_id: int,
        *,
        reason: str,
        evidence: list[str],
        context: VerificationContext | None = None,
    ) -> None:
        self._require_evidence(evidence)
        task = self.plan_store.get_task(task_id)
        if task is None or task.status is not TaskStatus.VERIFYING:
            raise VerificationError("task must be VERIFYING before BLOCKED")
        self.state_authority.finish_verification(
                target_type=VerificationTargetType.TASK,
                target_id=task_id,
                verification_status="BLOCKED",
                evidence=evidence,
                reason=reason,
                context=context,
                target_status=TaskStatus.BLOCKED.value,
        )
