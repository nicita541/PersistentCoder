from app.tasks.models import TaskDraft

class TaskBuilder:
    def build(self, components):
        return [TaskDraft(title=c, description=c, success_criteria=[f"{c} completed"]) for c in components]
