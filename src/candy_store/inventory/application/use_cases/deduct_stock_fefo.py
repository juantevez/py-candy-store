from dataclasses import dataclass

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.errors import InsufficientStockError
from candy_store.inventory.domain.value_objects import BatchAllocation, StockLocation
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class DeductStockFefoCommand:
    product_id: ProductId
    location: StockLocation
    quantity: int


class DeductStockFefoUseCase:
    def __init__(self, uow: InventoryUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: DeductStockFefoCommand) -> tuple[BatchAllocation, ...]:
        async with self._uow as uow:
            item = await uow.items.get(command.product_id, command.location)
            if item is None:
                raise InsufficientStockError(requested=command.quantity, available=0)
            allocations = item.deduct(command.quantity, self._clock.today())
            await uow.commit()
        return allocations
