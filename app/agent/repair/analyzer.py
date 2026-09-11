from app.tasks.models import ReplanScope

class FailureAnalyzer:
    def analyze(self, facts):
        verification=facts.get("verification",{})
        reason=verification.get("reason","verification failed") if isinstance(verification,dict) else "verification failed"
        return {"scope": ReplanScope.STEP, "reason": reason}
