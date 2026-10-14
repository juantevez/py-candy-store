from candy_store.shared.domain.events import DomainEvent


class AggregateRoot:
    """Raíz de agregado: frontera de consistencia transaccional.

    `version` habilita bloqueo optimista en persistencia: dos transacciones que
    modifiquen el mismo agregado en paralelo no pueden confirmar ambas.
    """

    def __init__(self, *, version: int = 0) -> None:
        self.version = version
        self._pending_events: list[DomainEvent] = []

    def _record(self, event: DomainEvent) -> None:
        self._pending_events.append(event)

    def pull_events(self) -> list[DomainEvent]:
        events, self._pending_events = self._pending_events, []
        return events
