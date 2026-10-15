import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from candy_store.sales.application.ports.payment_gateway import (
    CheckoutItem,
    CheckoutRequest,
    PaymentGateway,
    PaymentGatewayError,
)
from candy_store.sales.application.ports.product_catalog import ProductCatalog
from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.ports.stock import ItemQuantity, OutOfStockError, StockGateway
from candy_store.sales.application.use_cases._orders import (
    add_order,
    find_order,
    price_lines,
    stock_items,
    update_order,
)
from candy_store.sales.domain.errors import InvalidOrderError, InvalidOrderStateError
from candy_store.sales.domain.model import ONLINE_PAYMENT_METHODS, Order
from candy_store.sales.domain.value_objects import (
    CancellationReason,
    CheckoutSession,
    OrderId,
    OrderStatus,
    PaymentMethod,
    SalesChannel,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import Money

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckoutOnlineCartCommand:
    # Lo genera el frontend al iniciar el checkout: reintentar no crea otra orden.
    order_id: OrderId
    items: Sequence[ItemQuantity]
    customer_email: str | None = None
    payment_method: PaymentMethod = PaymentMethod.MERCADOPAGO


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    order_id: OrderId
    payment_method: PaymentMethod
    redirect_url: str | None  # solo Mercado Pago: a dónde redirigir al comprador
    expires_at: datetime
    total: Money


@dataclass(frozen=True, slots=True)
class HoldPolicy:
    """Cuánto tiempo se aparta el stock esperando cada medio de pago."""

    mercadopago: timedelta = timedelta(minutes=15)
    # Una transferencia no es instantánea: alguien tiene que verla acreditada.
    bank_transfer: timedelta = timedelta(hours=24)

    def for_method(self, method: PaymentMethod) -> timedelta:
        return self.bank_transfer if method is PaymentMethod.BANK_TRANSFER else self.mercadopago


class CheckoutOnlineCartUseCase:
    """Inicio del checkout web: reserva temporal de stock y, según el medio de pago,
    sesión de Mercado Pago o espera de la transferencia.

    Pasos (cada uno idempotente): crear la orden, apartar el stock, crear la
    preferencia de pago (solo Mercado Pago) y dejar la orden esperando el pago. Si
    la pasarela falla, se libera el stock y la orden se cancela.
    """

    def __init__(
        self,
        uow: SalesUnitOfWork,
        stock: StockGateway,
        catalog: ProductCatalog,
        payments: PaymentGateway,
        clock: Clock,
        holds: HoldPolicy | None = None,
    ) -> None:
        self._uow = uow
        self._stock = stock
        self._catalog = catalog
        self._payments = payments
        self._clock = clock
        self._holds = holds or HoldPolicy()

    @retry_on_concurrency()
    async def execute(self, command: CheckoutOnlineCartCommand) -> CheckoutResult:
        method = command.payment_method
        if method not in ONLINE_PAYMENT_METHODS:
            raise InvalidOrderError(f"Medio de pago no disponible para compras online: {method}")

        order = await find_order(self._uow, command.order_id)
        if order is None:
            order = Order.place(
                order_id=command.order_id,
                channel=SalesChannel.ONLINE,
                lines=await price_lines(self._catalog, command.items),
                placed_at=self._clock.now(),
                customer_email=command.customer_email,
            )
            await add_order(self._uow, order)

        if order.channel is not SalesChannel.ONLINE:
            raise InvalidOrderStateError(f"La orden {order.order_id} no es online")
        if order.status is OrderStatus.AWAITING_PAYMENT:
            return _result(order)
        if order.status is not OrderStatus.PLACED:
            raise InvalidOrderStateError(f"La orden {order.order_id} está {order.status}")

        try:
            expires_at = await self._stock.hold_for_online_order(
                order.order_id, stock_items(order), hold_for=self._holds.for_method(method)
            )
        except OutOfStockError:
            await self._cancel(order.order_id, CancellationReason.OUT_OF_STOCK)
            raise

        session = None
        if method is PaymentMethod.MERCADOPAGO:
            session = await self._create_payment_session(order, expires_at)

        awaiting = await update_order(
            self._uow,
            order.order_id,
            lambda o: o.await_payment(method, expires_at, checkout=session),
        )
        return _result(awaiting)

    async def _create_payment_session(self, order: Order, expires_at: datetime) -> CheckoutSession:
        try:
            return await self._payments.create_checkout(
                CheckoutRequest(
                    order_id=order.order_id,
                    items=tuple(
                        CheckoutItem(
                            product_id=line.product_id,
                            title=line.description,
                            quantity=line.quantity,
                            unit_price=line.unit_price,
                        )
                        for line in order.lines
                    ),
                    payer_email=order.customer_email,
                    expires_at=expires_at,
                )
            )
        except PaymentGatewayError:
            logger.exception("No se pudo crear el checkout de la orden %s", order.order_id)
            await self._stock.cancel_online_order(order.order_id)
            await self._cancel(order.order_id, CancellationReason.CHECKOUT_FAILED)
            raise

    async def _cancel(self, order_id: OrderId, reason: CancellationReason) -> None:
        await update_order(self._uow, order_id, lambda o: o.cancel(reason, self._clock.now()))


def _result(order: Order) -> CheckoutResult:
    if order.payment_method is None or order.hold_expires_at is None:
        raise InvalidOrderStateError(f"La orden {order.order_id} no tiene un checkout iniciado")
    return CheckoutResult(
        order_id=order.order_id,
        payment_method=order.payment_method,
        redirect_url=order.checkout.redirect_url if order.checkout else None,
        expires_at=order.hold_expires_at,
        total=order.total,
    )
