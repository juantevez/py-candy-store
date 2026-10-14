import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Self

from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryRepository,
    InventoryUnitOfWork,
    OperationLog,
)
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.clock import STORE_TIMEZONE

type _Key = tuple[ProductId, StockLocation]


@dataclass
class FixedClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        return self.current.astimezone(STORE_TIMEZONE).date()


@dataclass
class InMemoryInventoryRepository(InventoryRepository):
    items: dict[_Key, InventoryItem] = field(default_factory=dict)
    seen: set[_Key] = field(default_factory=set)

    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        item = self.items.get((product_id, location))
        if item is not None:
            self.seen.add((product_id, location))
        return item

    async def list_by_reservation(self, reference: StockReference) -> list[InventoryItem]:
        found = [item for item in self.items.values() if item.has_reservation(reference)]
        for item in found:
            self.seen.add((item.product_id, item.location))
        return found

    async def add(self, item: InventoryItem) -> None:
        key = (item.product_id, item.location)
        self.items[key] = item
        self.seen.add(key)

    async def keys_with_expired_reservations(self, now: datetime, limit: int) -> list[_Key]:
        return [
            key
            for key, item in self.items.items()
            if any(r.is_expired(now) for r in item.reservations)
        ][:limit]


@dataclass
class InMemoryOperationLog(OperationLog):
    entries: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    async def get(
        self, reference: StockReference, operation: InventoryOperation
    ) -> dict[str, Any] | None:
        return self.entries.get((reference.value, operation.value))

    async def add(
        self, reference: StockReference, operation: InventoryOperation, result: dict[str, Any]
    ) -> None:
        self.entries[(reference.value, operation.value)] = result


class FakeInventoryUnitOfWork(InventoryUnitOfWork):
    """UoW en memoria con semántica transaccional real: lo no confirmado se descarta."""

    def __init__(self) -> None:
        self.items = self._repo = InMemoryInventoryRepository()
        self.operations = self._log = InMemoryOperationLog()
        self.commits = 0
        self.published_events: list[DomainEvent] = []
        self._snapshot: tuple[dict[_Key, InventoryItem], dict[tuple[str, str], Any]] | None = None

    async def __aenter__(self) -> Self:
        self._repo.seen.clear()
        self._snapshot = (copy.deepcopy(self._repo.items), copy.deepcopy(self._log.entries))
        return self

    async def commit(self) -> None:
        self.commits += 1
        for key in self._repo.seen:
            self.published_events.extend(self._repo.items[key].pull_events())
        self._snapshot = None

    async def rollback(self) -> None:
        if self._snapshot is not None:
            self._repo.items, self._log.entries = self._snapshot
            self._snapshot = None
