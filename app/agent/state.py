
from dataclasses import dataclass, field
from enum import Enum

class AgentPhase(str, Enum):
    OBSERVE="observe"
    PLAN="plan"
    EXECUTE="execute"
    VERIFY="verify"
    REPAIR="repair"
    REMEMBER="remember"
    DONE="done"

@dataclass
class AgentState:
    request: str
    phase: AgentPhase = AgentPhase.OBSERVE
    plan_id: int | None = None
    task_id: int | None = None
    facts: dict[str, object] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
