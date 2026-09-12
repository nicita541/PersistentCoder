from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentMessage:
    sender: str
    receiver: str
    kind: str
    payload: dict[str, object] = field(
        default_factory=dict
    )
