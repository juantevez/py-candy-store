import logging

from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryUnitOfWork,
)
from candy_store.inventory.domain.errors import ReservationNotFoundError
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.application.unit_of_work import ConcurrencyError
from candy_store.shared.domain.value_objects import ProductId

logger = logging.getLogger(__name__)


class ReleaseReservationUseCase:
    """Checkout abandonado o pago rechazado: devuelve el stock reservado de la orden."""

    def __init__(self, uow: InventoryUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, reference: StockReference) -> None:
        async with self._uow as uow:
            operation = InventoryOperation.RESERVATION_RELEASE
            if await uow.operations.get(reference, operation) is not None:
                return
            items = await uow.items.list_by_reservation(reference)
            if not items:
                raise ReservationNotFoundError(f"No existe una reserva para {reference}")
            for item in items:
                item.release_reservation(reference)
            await uow.operations.add(reference, operation, {})
            await uow.commit()


class ReleaseExpiredReservationsUseCase:
    """Proceso periódico: libera las reservas cuyo TTL venció.

    Usa una transacción por agregado para no bloquear las ventas de muchos
    productos a la vez. Si pierde una carrera con una venta, ese producto se
    reintenta en la próxima ejecución.
    """

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock, *, batch_size: int = 100) -> None:
        self._uow = uow
        self._clock = clock
        self._batch_size = batch_size

    async def execute(self) -> int:
        now = self._clock.now()
        async with self._uow as uow:
            keys = await uow.items.keys_with_expired_reservations(now, self._batch_size)

        released = 0
        for product_id, location in keys:
            try:
                released += await self._release_for(product_id, location)
            except ConcurrencyError:
                logger.info("Conflicto liberando reservas de %s/%s", product_id, location)
        return released

    async def _release_for(self, product_id: ProductId, location: StockLocation) -> int:
        async with self._uow as uow:
            item = await uow.items.get(product_id, location)
            if item is None:
                return 0
            released = item.release_expired_reservations(self._clock.now())
            await uow.commit()
        return len(released)
