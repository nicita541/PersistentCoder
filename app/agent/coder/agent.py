
class CodingAgent:
    def __init__(self, executor):
        self.executor=executor

    def execute(self, plan):
        return self.executor.execute(plan)
