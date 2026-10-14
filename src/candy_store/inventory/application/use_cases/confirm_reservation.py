from candy_store.inventory.application.dto import DeductionResult, LineAllocations
from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryUnitOfWork,
)
from candy_store.inventory.domain.errors import (
    InsufficientStockError,
    ReservationNotFoundError,
    Shortage,
    StockShortageError,
)
from candy_store.inventory.domain.value_objects import StockReference
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


class ConfirmReservationUseCase:
    """Pago aprobado: todas las reservas de la orden se convierten en salida por FEFO.

    Todo o nada: si alguna línea ya no puede cubrirse (roturas o vencimientos
    desde la reserva), no se descuenta nada y las reservas quedan intactas.
    Idempotente por referencia (webhooks de pago duplicados).
    """

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, reference: StockReference) -> DeductionResult:
        today = self._clock.today()
        async with self._uow as uow:
            operation = InventoryOperation.RESERVATION_CONFIRMATION
            stored = await uow.operations.get(reference, operation)
            if stored is not None:
                return DeductionResult.from_json(stored)

            items = await uow.items.list_by_reservation(reference)
            if not items:
                raise ReservationNotFoundError(f"No existe una reserva para {reference}")

            lines: list[LineAllocations] = []
            shortages: list[Shortage] = []
            for item in sorted(items, key=lambda i: str(i.product_id)):
                try:
                    allocations = item.confirm_reservation(reference, today)
                except InsufficientStockError as exc:
                    shortages.append(Shortage(item.product_id, exc.requested, exc.available))
                else:
                    lines.append(LineAllocations(item.product_id, allocations))
            if shortages:
                raise StockShortageError(tuple(shortages))

            result = DeductionResult(tuple(lines))
            await uow.operations.add(reference, operation, result.to_json())
            await uow.commit()
        return result
