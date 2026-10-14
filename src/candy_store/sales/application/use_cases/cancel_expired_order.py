from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.domain.value_objects import CancellationReason, OrderId, OrderStatus
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


class CancelExpiredOrderUseCase:
    """Reacciona al vencimiento de la reserva de stock de una orden online.

    Idempotente: inventory avisa una vez por producto de la orden, y solo la
    primera notificación cambia algo.
    """

    def __init__(self, uow: SalesUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, order_id: OrderId) -> bool:
        async with self._uow as uow:
            order = await uow.orders.get(order_id)
            if order is None or order.status not in {
                OrderStatus.PLACED,
                OrderStatus.AWAITING_PAYMENT,
            }:
                return False
            order.cancel(CancellationReason.HOLD_EXPIRED, self._clock.now())
            await uow.commit()
        return True
