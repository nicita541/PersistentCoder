from __future__ import annotations

import re

from app.tasks.attempt_store import (
    AttemptStore,
)
from app.tasks.models import (
    AttemptStatus,
    ReplanDecision,
    ReplanScope,
    ReplanTargetType,
    ReplanTrigger,
    TaskStatus,
)
from app.tasks.replan_store import (
    ReplanStore,
)
from app.tasks.step_store import (
    StepStore,
)
from app.tasks.store import (
    PlanStore,
)


class ReplanError(
    RuntimeError
):
    pass


class Replanner:
    DEFAULT_MAX_STEP_ATTEMPTS = 3

    def __init__(
        self,
        *,
        plan_store: PlanStore,
        step_store: StepStore,
        attempt_store: AttemptStore,
        replan_store: ReplanStore,
        max_step_attempts: int = (
            DEFAULT_MAX_STEP_ATTEMPTS
        ),
    ) -> None:
        if max_step_attempts < 1:
            raise ValueError(
                "max_step_attempts "
                "must be at least 1"
            )

        self.plan_store = plan_store
        self.step_store = step_store
        self.attempt_store = (
            attempt_store
        )
        self.replan_store = (
            replan_store
        )

        self.max_step_attempts = (
            max_step_attempts
        )

    @staticmethod
    def _normalize_approach(
        value: str,
    ) -> str:
        value = value.casefold()

        value = re.sub(
            r"\s+",
            " ",
            value,
        )

        return value.strip()

    @staticmethod
    def _validate_reason(
        reason: str,
    ) -> str:
        reason = reason.strip()

        if not reason:
            raise ReplanError(
                "replan reason is required"
            )

        return reason

    def _record(
        self,
        *,
        scope: ReplanScope,
        trigger: ReplanTrigger,
        target_type: ReplanTargetType,
        target_id: int,
        reason: str,
    ) -> ReplanDecision:
        decision = ReplanDecision(
            scope=scope,
            trigger=trigger,
            target_type=target_type,
            target_id=target_id,
            reason=self._validate_reason(
                reason
            ),
        )

        self.replan_store.record(
            decision
        )

        return decision

    # ==========================================
    # APPROACH GUARD
    # ==========================================

    def assert_step_approach_allowed(
        self,
        step_id: int,
        approach: str,
    ) -> None:
        step = self.step_store.get_step(
            step_id
        )

        if step is None:
            raise ReplanError(
                f"unknown step: {step_id}"
            )

        normalized = (
            self._normalize_approach(
                approach
            )
        )

        if not normalized:
            raise ReplanError(
                "approach is required"
            )

        previous_attempts = (
            self.attempt_store
            .get_step_attempts(
                step_id
            )
        )

        for attempt in previous_attempts:
            if attempt.status not in {
                AttemptStatus.FAILED,
                AttemptStatus.BLOCKED,
            }:
                continue

            if attempt.approach is None:
                continue

            previous_approach = (
                self._normalize_approach(
                    attempt.approach
                )
            )

            if (
                previous_approach
                == normalized
            ):
                raise ReplanError(
                    "this approach already "
                    "failed for this step"
                )

    # ==========================================
    # STEP FAILURE
    # ==========================================

    def decide_step_failure(
        self,
        step_id: int,
        *,
        reason: str,
    ) -> ReplanDecision:
        step = self.step_store.get_step(
            step_id
        )

        if step is None:
            raise ReplanError(
                f"unknown step: {step_id}"
            )

        previous_attempts = (
            self.attempt_store
            .get_step_attempts(
                step_id
            )
        )

        failed_attempts = [
            attempt
            for attempt in previous_attempts
            if attempt.status
            in {
                AttemptStatus.FAILED,
                AttemptStatus.BLOCKED,
            }
        ]

        if (
            len(failed_attempts)
            >= self.max_step_attempts
        ):
            return self._record(
                scope=ReplanScope.TASK,
                trigger=(
                    ReplanTrigger.ATTEMPT_LIMIT
                ),
                target_type=(
                    ReplanTargetType.TASK
                ),
                target_id=step.task_id,
                reason=reason,
            )

        return self._record(
            scope=ReplanScope.STEP,
            trigger=(
                ReplanTrigger.VERIFICATION_FAIL
            ),
            target_type=(
                ReplanTargetType.STEP
            ),
            target_id=step_id,
            reason=reason,
        )

    # ==========================================
    # TASK FAILURE
    # ==========================================

    def decide_task_failure(
        self,
        task_id: int,
        *,
        reason: str,
    ) -> ReplanDecision:
        task = self.plan_store.get_task(
            task_id
        )

        if task is None:
            raise ReplanError(
                f"unknown task: {task_id}"
            )

        if (
            task.status
            is TaskStatus.DONE
        ):
            raise ReplanError(
                "DONE task cannot be "
                "reopened by replanner"
            )

        if (
            task.status
            is TaskStatus.SUPERSEDED
        ):
            raise ReplanError(
                "SUPERSEDED task cannot "
                "be replanned"
            )

        return self._record(
            scope=ReplanScope.TASK,
            trigger=(
                ReplanTrigger.VERIFICATION_FAIL
            ),
            target_type=(
                ReplanTargetType.TASK
            ),
            target_id=task_id,
            reason=reason,
        )

    # ==========================================
    # DEPENDENCY FAILURE
    # ==========================================

    def decide_dependency_failure(
        self,
        task_id: int,
        *,
        reason: str,
    ) -> ReplanDecision:
        task = self.plan_store.get_task(
            task_id
        )

        if task is None:
            raise ReplanError(
                f"unknown task: {task_id}"
            )

        if (
            task.status
            is TaskStatus.DONE
        ):
            raise ReplanError(
                "DONE task cannot be "
                "reopened by replanner"
            )

        return self._record(
            scope=ReplanScope.TASK,
            trigger=(
                ReplanTrigger
                .DEPENDENCY_FAILURE
            ),
            target_type=(
                ReplanTargetType.TASK
            ),
            target_id=task_id,
            reason=reason,
        )

    # ==========================================
    # GLOBAL REPLAN
    # ==========================================

    def decide_global_replan(
        self,
        plan_id: int,
        *,
        reason: str,
        trigger: ReplanTrigger,
    ) -> ReplanDecision:
        plan = self.plan_store.get_plan(
            plan_id
        )

        if plan is None:
            raise ReplanError(
                f"unknown plan: {plan_id}"
            )

        if trigger not in {
            ReplanTrigger.USER_CHANGE,
            ReplanTrigger.INVALID_ASSUMPTION,
        }:
            raise ReplanError(
                "global replan requires "
                "USER_CHANGE or "
                "INVALID_ASSUMPTION"
            )

        return self._record(
            scope=ReplanScope.GLOBAL,
            trigger=trigger,
            target_type=(
                ReplanTargetType.PLAN
            ),
            target_id=plan_id,
            reason=reason,
        )