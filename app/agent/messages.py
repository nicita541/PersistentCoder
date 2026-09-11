
from dataclasses import dataclass

@dataclass
class AgentMessage:
    sender: str
    receiver: str
    kind: str
    payload: dict[str, object]
