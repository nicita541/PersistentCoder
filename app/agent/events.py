from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


@dataclass
class AgentEvent:
    name: str
    payload: dict[str, object] = field(
        default_factory=dict
    )
    created_at: str = field(
        default_factory=lambda: datetime.now(
            timezone.utc
        ).isoformat()
    )


EventHandler = Callable[[AgentEvent], None]


class EventBus:
    """
    Минимальная шина событий Agent Layer.

    AgentController публикует события, наблюдатели
    (UI, логгеры, тесты) подписываются.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.events: list[AgentEvent] = []

    def subscribe(
        self,
        handler: EventHandler,
    ) -> None:
        self._handlers.append(handler)

    def emit(
        self,
        name: str,
        payload: dict[str, object] | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            name=name,
            payload=dict(payload or {}),
        )

        self.events.append(event)

        for handler in self._handlers:
            handler(event)

        return event
