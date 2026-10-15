"""Confirmación manual de una transferencia bancaria (la hace el personal)."""

import logging
from dataclasses import dataclass

from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.ports.stock import (
    OutOfStockError,
    StockGateway,
    StockHoldLostError,
)
from candy_store.sales.application.use_cases._orders import find_order, update_order
from candy_store.sales.application.use_cases.get_order import OrderView
from candy_store.sales.domain.errors import InvalidOrderStateError, OrderNotFoundError
from candy_store.sales.domain.value_objects import (
    OrderId,
    OrderStatus,
    PaymentMethod,
    RefundReason,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfirmBankTransferCommand:
    order_id: OrderId
    # Número de operación del banco, si se quiere dejar registrado.
    bank_reference: str | None = None


class ConfirmBankTransferUseCase:
    """El personal vio la transferencia acreditada: el stock reservado sale y la orden
    queda pagada. Es idempotente.

    Mismas reglas que un pago de Mercado Pago que llega tarde: si la orden ya se
    canceló (venció la reserva) o el stock ya no está, el dinero se recibió pero no se
    puede entregar, y la orden queda para devolver la transferencia.
    """

    def __init__(self, uow: SalesUnitOfWork, stock: StockGateway, clock: Clock) -> None:
        self._uow = uow
        self._stock = stock
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: ConfirmBankTransferCommand) -> OrderView:
        order = await find_order(self._uow, command.order_id)
        if order is None:
            raise OrderNotFoundError(f"No existe la orden {command.order_id}")
        if order.payment_method is not PaymentMethod.BANK_TRANSFER:
            raise InvalidOrderStateError(f"La orden {order.order_id} no se paga por transferencia")
        if order.status in {OrderStatus.PAID, OrderStatus.REFUND_REQUIRED}:
            return OrderView.of(order)  # ya se había confirmado

        reference = command.bank_reference or "transferencia"
        if order.status is OrderStatus.CANCELLED:
            return await self._require_refund(
                order.order_id, reference, RefundReason.ORDER_CANCELLED
            )
        if order.status is not OrderStatus.AWAITING_PAYMENT:
            raise InvalidOrderStateError(f"La orden {order.order_id} está {order.status}")

        try:
            await self._stock.commit_online_order(order.order_id)
        except (StockHoldLostError, OutOfStockError):
            return await self._require_refund(
                order.order_id, reference, RefundReason.STOCK_UNAVAILABLE
            )
        paid = await update_order(
            self._uow,
            order.order_id,
            lambda o: o.confirm_transfer(command.bank_reference, self._clock.now()),
        )
        return OrderView.of(paid)

    async def _require_refund(
        self, order_id: OrderId, reference: str, reason: RefundReason
    ) -> OrderView:
        order = await update_order(
            self._uow, order_id, lambda o: o.require_refund(reference, reason, self._clock.now())
        )
        logger.warning("La orden %s requiere devolver la transferencia (%s)", order_id, reason)
        return OrderView.of(order)
