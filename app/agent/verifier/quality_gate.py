class QualityGate:
    def __init__(self, verifier=None):
        self.verifier=verifier

    def check(self, result):
        if self.verifier and hasattr(result,"task_id"):
            return self.verifier
        ok=bool(result and result.get("ok") is True)
        return {"ok":ok,"evidence":["execution_result"] if ok else ["execution_failed"]}
