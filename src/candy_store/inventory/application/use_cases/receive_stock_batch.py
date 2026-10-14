from dataclasses import dataclass
from datetime import date

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class ReceiveStockBatchCommand:
    product_id: ProductId
    location: StockLocation
    lot_number: str
    expiration: date
    quantity: int


class ReceiveStockBatchUseCase:
    def __init__(self, uow: InventoryUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def execute(self, command: ReceiveStockBatchCommand) -> BatchId:
        batch_id = BatchId.new()
        async with self._uow as uow:
            item = await uow.items.get(command.product_id, command.location)
            if item is None:
                item = InventoryItem(product_id=command.product_id, location=command.location)
                await uow.items.add(item)
            item.receive_batch(
                batch_id=batch_id,
                lot_number=LotNumber(command.lot_number),
                expiration=ExpirationDate(command.expiration),
                quantity=command.quantity,
                received_at=self._clock.now(),
                today=self._clock.today(),
            )
            await uow.commit()
        return batch_id
