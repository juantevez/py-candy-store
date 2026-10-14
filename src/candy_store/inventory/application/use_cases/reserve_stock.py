from dataclasses import dataclass
from datetime import datetime, timedelta

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.errors import InsufficientStockError
from candy_store.inventory.domain.value_objects import ReservationId, StockLocation
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class ReserveStockCommand:
    product_id: ProductId
    location: StockLocation
    quantity: int


@dataclass(frozen=True, slots=True)
class ReservationReceipt:
    reservation_id: ReservationId
    expires_at: datetime


class ReserveStockUseCase:
    """Aparta stock durante el checkout online hasta que se confirme el pago."""

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock, ttl: timedelta) -> None:
        self._uow = uow
        self._clock = clock
        self._ttl = ttl

    @retry_on_concurrency()
    async def execute(self, command: ReserveStockCommand) -> ReservationReceipt:
        receipt = ReservationReceipt(
            reservation_id=ReservationId.new(), expires_at=self._clock.now() + self._ttl
        )
        async with self._uow as uow:
            item = await uow.items.get(command.product_id, command.location)
            if item is None:
                raise InsufficientStockError(requested=command.quantity, available=0)
            item.reserve(
                reservation_id=receipt.reservation_id,
                quantity=command.quantity,
                expires_at=receipt.expires_at,
                today=self._clock.today(),
            )
            await uow.commit()
        return receipt
