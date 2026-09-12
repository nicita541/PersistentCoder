from __future__ import annotations

from app.agent.repair.analyzer import (
    FailureAnalysis,
)


RETRY_STEP = "RETRY_STEP"
REPLAN_TASK = "REPLAN_TASK"
GIVE_UP = "GIVE_UP"


class RepairStrategySelector:
    """
    Эскалация scope строго по попыткам:

    сначала повтор шага,
    затем replan задачи,
    затем отказ.
    """

    def __init__(
        self,
        *,
        max_step_attempts: int = 2,
        max_task_attempts: int = 3,
    ) -> None:
        if max_step_attempts < 1:
            raise ValueError(
                "max_step_attempts "
                "must be at least 1"
            )

        if max_task_attempts < 1:
            raise ValueError(
                "max_task_attempts "
                "must be at least 1"
            )

        self.max_step_attempts = (
            max_step_attempts
        )
        self.max_task_attempts = (
            max_task_attempts
        )

    def select(
        self,
        *,
        analysis: FailureAnalysis,
        step_attempts: int,
        task_attempts: int,
    ) -> str:
        if step_attempts < self.max_step_attempts:
            return RETRY_STEP

        if task_attempts < self.max_task_attempts:
            return REPLAN_TASK

        return GIVE_UP

