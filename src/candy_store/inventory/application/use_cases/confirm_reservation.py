from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.errors import ReservationNotFoundError
from candy_store.inventory.domain.value_objects import BatchAllocation, ReservationId
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


class ConfirmReservationUseCase:
    """Pago aprobado: la reserva se convierte en salida de stock por FEFO."""

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, reservation_id: ReservationId) -> tuple[BatchAllocation, ...]:
        async with self._uow as uow:
            item = await uow.items.get_by_reservation(reservation_id)
            if item is None:
                raise ReservationNotFoundError(f"No existe la reserva {reservation_id}")
            allocations = item.confirm_reservation(reservation_id, self._clock.today())
            await uow.commit()
        return allocations
