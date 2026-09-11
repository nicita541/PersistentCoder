class RepairStrategySelector:
    def select(self, failure):
        return failure.get("scope") if isinstance(failure,dict) else None
