
class RepairAgent:
    def __init__(self, analyzer):
        self.analyzer=analyzer

    def repair(self, facts):
        return self.analyzer.analyze(facts)
