"""Bus de eventos en memoria para desacoplar contextos dentro del monolito.

Los handlers reciben el payload JSON (no la clase del evento) para que un
contexto no tenga que importar el dominio de otro.
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

type EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: defaultdict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        for handler in self._handlers.get(event_type, []):
            await handler(payload)
