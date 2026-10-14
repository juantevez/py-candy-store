"""Procesamiento idempotente de notificaciones de pago."""

import logging
from enum import StrEnum

from candy_store.sales.application.ports.payment_gateway import PaymentGateway, PaymentStatus
from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.ports.stock import (
    OutOfStockError,
    StockGateway,
    StockHoldLostError,
)
from candy_store.sales.application.use_cases._orders import find_order, update_order
from candy_store.sales.domain.value_objects import (
    OrderId,
    OrderStatus,
    RefundReason,
    SalesChannel,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency

logger = logging.getLogger(__name__)


class PaymentNotificationOutcome(StrEnum):
    IGNORED = "ignored"  # no corresponde a una orden online nuestra
    NOT_APPROVED = "not_approved"  # pendiente o rechazado: la orden sigue esperando
    ALREADY_PROCESSED = "already_processed"
    PAID = "paid"
    REFUND_REQUIRED = "refund_required"


class HandlePaymentNotificationUseCase:
    """La notificación solo dice "cambió el pago X": el estado real se consulta a
    la pasarela. Repetir la notificación no tiene efecto (webhooks duplicados).

    Un pago rechazado no cancela la orden: en Checkout Pro el comprador puede
    reintentar con otro medio mientras la reserva esté vigente. Si nunca se
    aprueba, la orden se cancela cuando vence la reserva de stock.
    """

    def __init__(
        self,
        uow: SalesUnitOfWork,
        stock: StockGateway,
        payments: PaymentGateway,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._stock = stock
        self._payments = payments
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, payment_id: str) -> PaymentNotificationOutcome:
        payment = await self._payments.get_payment(payment_id)
        if payment.order_id is None:
            return PaymentNotificationOutcome.IGNORED
        order = await find_order(self._uow, payment.order_id)
        if order is None or order.channel is not SalesChannel.ONLINE:
            logger.warning("Pago %s de una orden desconocida %s", payment_id, payment.order_id)
            return PaymentNotificationOutcome.IGNORED
        if payment.status is not PaymentStatus.APPROVED:
            return PaymentNotificationOutcome.NOT_APPROVED

        match order.status:
            case OrderStatus.PAID | OrderStatus.REFUND_REQUIRED:
                if order.payment_id != payment_id:
                    # Segundo pago aprobado para la misma orden: requiere intervención manual.
                    logger.error("Pago duplicado %s para la orden %s", payment_id, order.order_id)
                return PaymentNotificationOutcome.ALREADY_PROCESSED
            case OrderStatus.CANCELLED:
                return await self._require_refund(
                    order.order_id, payment_id, RefundReason.ORDER_CANCELLED
                )
            case OrderStatus.AWAITING_PAYMENT:
                pass
            case _:
                logger.warning(
                    "Pago %s para la orden %s en estado %s",
                    payment_id,
                    order.order_id,
                    order.status,
                )
                return PaymentNotificationOutcome.IGNORED

        if payment.amount != order.total:
            await self._stock.cancel_online_order(order.order_id)
            return await self._require_refund(
                order.order_id, payment_id, RefundReason.AMOUNT_MISMATCH
            )
        try:
            await self._stock.commit_online_order(order.order_id)
        except (StockHoldLostError, OutOfStockError):
            return await self._require_refund(
                order.order_id, payment_id, RefundReason.STOCK_UNAVAILABLE
            )
        await update_order(
            self._uow,
            order.order_id,
            lambda o: o.mark_paid(payment_id, payment.amount, self._clock.now()),
        )
        return PaymentNotificationOutcome.PAID

    async def _require_refund(
        self, order_id: OrderId, payment_id: str, reason: RefundReason
    ) -> PaymentNotificationOutcome:
        await update_order(
            self._uow, order_id, lambda o: o.require_refund(payment_id, reason, self._clock.now())
        )
        logger.warning(
            "La orden %s requiere devolver el pago %s (%s)", order_id, payment_id, reason
        )
        return PaymentNotificationOutcome.REFUND_REQUIRED
