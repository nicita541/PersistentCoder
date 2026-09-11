from __future__ import annotations

from pathlib import Path

from app.llm.client import QwenClient
from app.tasks.models import PlanDraft
from app.tasks.planner_tool_loop import (
    ToolPlanner,
)
from app.tasks.store import PlanStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]

PLANNER_TRACE_PATH = (
    PROJECT_ROOT
    / "data"
    / "logs"
    / "planner_trace.log"
)


class PlannerRuntime:
    def __init__(self) -> None:
        self.llm = QwenClient()

        self.planner = ToolPlanner(
            self.llm,
            max_tool_steps=24,
            trace_path=PLANNER_TRACE_PATH,
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
