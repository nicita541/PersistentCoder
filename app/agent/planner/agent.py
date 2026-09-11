from app.tasks.models import PlanDraft

class PlannerAgent:
    def __init__(self, planner, task_store, builders=None):
        self.planner=planner
        self.task_store=task_store
        self.builders=builders or {}

    def plan(self, request):
        draft=self.planner.plan(request)
        if not isinstance(draft, PlanDraft):
            raise TypeError("planner must return PlanDraft")
        plan_id=self.task_store.create_plan(draft)
        return {"plan_id": plan_id, "draft": draft}
