from dataclasses import dataclass

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.errors import BatchNotFoundError
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import AdjustmentReason, BatchId, StockLocation
from candy_store.shared.domain.value_objects import ProductId


async def _get_item_or_fail(
    uow: InventoryUnitOfWork, product_id: ProductId, location: StockLocation, batch_id: BatchId
) -> InventoryItem:
    item = await uow.items.get(product_id, location)
    if item is None:
        raise BatchNotFoundError(f"No existe el lote {batch_id}")
    return item


@dataclass(frozen=True, slots=True)
class RegisterLossCommand:
    product_id: ProductId
    location: StockLocation
    batch_id: BatchId
    quantity: int
    reason: AdjustmentReason


class RegisterLossUseCase:
    """Merma o rotura sobre un lote."""

    def __init__(self, uow: InventoryUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, command: RegisterLossCommand) -> None:
        async with self._uow as uow:
            item = await _get_item_or_fail(
                uow, command.product_id, command.location, command.batch_id
            )
            item.register_loss(
                batch_id=command.batch_id, quantity=command.quantity, reason=command.reason
            )
            await uow.commit()


@dataclass(frozen=True, slots=True)
class RegisterAuditCountCommand:
    product_id: ProductId
    location: StockLocation
    batch_id: BatchId
    counted_quantity: int


class RegisterAuditCountUseCase:
    """Ajuste por conteo físico de auditoría. Devuelve la diferencia aplicada."""

    def __init__(self, uow: InventoryUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, command: RegisterAuditCountCommand) -> int:
        async with self._uow as uow:
            item = await _get_item_or_fail(
                uow, command.product_id, command.location, command.batch_id
            )
            delta = item.register_audit_count(
                batch_id=command.batch_id, counted_quantity=command.counted_quantity
            )
            await uow.commit()
        return delta
