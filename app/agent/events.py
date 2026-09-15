from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from app.agent.event_sanitizer import sanitize_event_payload


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

    def __init__(self, *, max_events: int = 1000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.max_events = max_events
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
            payload=dict(sanitize_event_payload(payload or {})),
        )

        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]

        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                continue

        return event
