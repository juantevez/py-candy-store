from dataclasses import dataclass
from datetime import date

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import BatchId, StockLocation
from candy_store.shared.application.clock import Clock
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class BatchView:
    batch_id: BatchId
    lot_number: str
    expiration: date
    quantity_on_hand: int
    expired: bool


@dataclass(frozen=True, slots=True)
class LocationStock:
    location: StockLocation
    on_hand: int
    sellable: int
    reserved: int
    available: int
    batches: tuple[BatchView, ...]


@dataclass(frozen=True, slots=True)
class StockSummary:
    product_id: ProductId
    locations: tuple[LocationStock, ...]

    @property
    def total_available(self) -> int:
        return sum(location.available for location in self.locations)


class GetStockSummaryUseCase:
    """Consulta de stock de un producto en todas las ubicaciones (solo lectura)."""

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, product_id: ProductId) -> StockSummary:
        today = self._clock.today()
        locations: list[LocationStock] = []
        async with self._uow as uow:
            for location in StockLocation:
                item = await uow.items.get(product_id, location)
                if item is not None:
                    locations.append(_location_stock(item, today))
        return StockSummary(product_id=product_id, locations=tuple(locations))


def _location_stock(item: InventoryItem, today: date) -> LocationStock:
    batches = sorted(
        (b for b in item.batches if b.quantity_on_hand > 0),
        key=lambda b: (b.expiration, b.received_at),
    )
    return LocationStock(
        location=item.location,
        on_hand=item.quantity_on_hand(),
        sellable=item.sellable_quantity(today),
        reserved=item.reserved_quantity(),
        available=item.available_quantity(today),
        batches=tuple(
            BatchView(
                batch_id=b.batch_id,
                lot_number=b.lot_number.value,
                expiration=b.expiration.value,
                quantity_on_hand=b.quantity_on_hand,
                expired=b.is_expired(today),
            )
            for b in batches
        ),
    )
