
class VerificationAgent:
    def __init__(self, quality_gate):
        self.quality_gate=quality_gate

    def verify(self, result):
        return self.quality_gate.check(result)
