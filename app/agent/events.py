
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class AgentEvent:
    name: str
    payload: dict[str, object]
    created_at: str = datetime.now(timezone.utc).isoformat()
