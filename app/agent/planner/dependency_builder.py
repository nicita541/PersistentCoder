from app.tasks.dependencies import validate_task_graph

class DependencyBuilder:
    def build(self, tasks):
        validate_task_graph(tasks)
        return tasks
