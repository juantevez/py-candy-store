from dataclasses import dataclass, field
from datetime import date, datetime

from candy_store.inventory.application.ports.repository import (
    InventoryRepository,
    InventoryUnitOfWork,
)
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import ReservationId, StockLocation
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.clock import STORE_TIMEZONE


@dataclass
class FixedClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        return self.current.astimezone(STORE_TIMEZONE).date()


@dataclass
class InMemoryInventoryRepository(InventoryRepository):
    items: dict[tuple[ProductId, StockLocation], InventoryItem] = field(default_factory=dict)
    seen: set[tuple[ProductId, StockLocation]] = field(default_factory=set)

    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        item = self.items.get((product_id, location))
        if item is not None:
            self.seen.add((product_id, location))
        return item

    async def get_by_reservation(self, reservation_id: ReservationId) -> InventoryItem | None:
        for item in self.items.values():
            if any(r.reservation_id == reservation_id for r in item.reservations):
                return await self.get(item.product_id, item.location)
        return None

    async def add(self, item: InventoryItem) -> None:
        key = (item.product_id, item.location)
        self.items[key] = item
        self.seen.add(key)

    async def keys_with_expired_reservations(
        self, now: datetime, limit: int
    ) -> list[tuple[ProductId, StockLocation]]:
        return [
            key
            for key, item in self.items.items()
            if any(r.is_expired(now) for r in item.reservations)
        ][:limit]


class FakeInventoryUnitOfWork(InventoryUnitOfWork):
    def __init__(self) -> None:
        self.items = self._repo = InMemoryInventoryRepository()
        self.commits = 0
        self.published_events: list[DomainEvent] = []

    async def commit(self) -> None:
        self.commits += 1
        for key in self._repo.seen:
            self.published_events.extend(self._repo.items[key].pull_events())

    async def rollback(self) -> None:
        pass
