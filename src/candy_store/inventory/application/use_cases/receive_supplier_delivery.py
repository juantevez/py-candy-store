from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryUnitOfWork,
)
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class DeliveryLine:
    product_id: ProductId
    lot_number: str
    expiration: date
    quantity: int


@dataclass(frozen=True, slots=True)
class ReceiveSupplierDeliveryCommand:
    reference: StockReference  # id del remito recibido en compras
    location: StockLocation
    received_at: datetime
    # Fecha local de recepción: el vencimiento se valida contra ella y no contra
    # el momento (posterior) en que se procesa el evento.
    received_on: date
    lines: Sequence[DeliveryLine]


class ReceiveSupplierDeliveryUseCase:
    """Ingresa al stock los lotes de un remito ya aceptado por compras.

    Idempotente por remito: el evento puede llegar más de una vez (outbox
    at-least-once) y los lotes se crean una sola vez.
    """

    def __init__(self, uow: InventoryUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, command: ReceiveSupplierDeliveryCommand) -> int:
        operation = InventoryOperation.SUPPLIER_DELIVERY
        async with self._uow as uow:
            stored = await uow.operations.get(command.reference, operation)
            if stored is not None:
                received: int = stored["batches"]
                return received

            for line in command.lines:
                item = await uow.items.get(line.product_id, command.location)
                if item is None:
                    item = InventoryItem(product_id=line.product_id, location=command.location)
                    await uow.items.add(item)
                item.receive_batch(
                    batch_id=BatchId.new(),
                    lot_number=LotNumber(line.lot_number),
                    expiration=ExpirationDate(line.expiration),
                    quantity=line.quantity,
                    received_at=command.received_at,
                    today=command.received_on,
                )
            await uow.operations.add(command.reference, operation, {"batches": len(command.lines)})
            await uow.commit()
        return len(command.lines)
