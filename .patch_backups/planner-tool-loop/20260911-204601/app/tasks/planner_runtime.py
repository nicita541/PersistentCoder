from __future__ import annotations

from app.llm.client import QwenClient
from app.tasks.ai_planner import AIPlanner
from app.tasks.models import PlanDraft
from app.tasks.store import PlanStore


class PlannerRuntime:
    def __init__(self) -> None:
        self.llm = QwenClient()

        self.planner = AIPlanner(
            self.llm,
            max_repair_attempts=2,
        )

        self.store = PlanStore()

    def create_plan(
        self,
        user_request: str,
    ) -> tuple[int, PlanDraft]:
        plan = self.planner.plan(
            user_request
        )

        plan_id = self.store.create_plan(
            plan
        )

        return plan_id, plan