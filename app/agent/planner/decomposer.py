
class TaskDecomposer:
    def decompose(self, decision):
        return decision.get("components", [])
