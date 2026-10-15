"""Agregado Order: una venta de mostrador o una orden online."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from candy_store.sales.domain.errors import (
    EmptyOrderError,
    InvalidOrderError,
    InvalidOrderStateError,
    PaymentAmountMismatchError,
)
from candy_store.sales.domain.events import (
    CounterSaleCompleted,
    OrderAwaitingPayment,
    OrderCancelled,
    OrderPaid,
    OrderPlaced,
    OrderRefundRequired,
)
from candy_store.sales.domain.value_objects import (
    CancellationReason,
    CheckoutSession,
    OrderId,
    OrderLine,
    OrderStatus,
    PaymentMethod,
    RefundReason,
    SalesChannel,
)
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.domain.value_objects import Money

_CANCELLABLE = {OrderStatus.PLACED, OrderStatus.AWAITING_PAYMENT}
ONLINE_PAYMENT_METHODS = frozenset({PaymentMethod.MERCADOPAGO, PaymentMethod.BANK_TRANSFER})


class Order(AggregateRoot):
    """Ciclo de vida:

    Mostrador: PLACED -> COMPLETED                 (o CANCELLED si no hay stock)
    Online:    PLACED -> AWAITING_PAYMENT -> PAID  (Mercado Pago o transferencia;
               o CANCELLED por stock, falla del
               checkout o vencimiento de la reserva; REFUND_REQUIRED si el pago se
               aprueba pero no se puede entregar)

    Las líneas y precios quedan congelados al crear la orden.
    """

    def __init__(
        self,
        *,
        order_id: OrderId,
        channel: SalesChannel,
        lines: Sequence[OrderLine],
        status: OrderStatus,
        placed_at: datetime,
        customer_email: str | None = None,
        payment_method: PaymentMethod | None = None,
        checkout: CheckoutSession | None = None,
        hold_expires_at: datetime | None = None,
        payment_id: str | None = None,
        cancellation_reason: CancellationReason | None = None,
        refund_reason: RefundReason | None = None,
        closed_at: datetime | None = None,
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        if not lines:
            raise EmptyOrderError("La orden debe tener al menos una línea")
        product_ids = [line.product_id for line in lines]
        if len(set(product_ids)) != len(product_ids):
            raise InvalidOrderError("Cada producto debe aparecer en una sola línea")
        if len({line.unit_price.currency for line in lines}) != 1:
            raise InvalidOrderError("Todas las líneas deben estar en la misma moneda")
        self.order_id = order_id
        self.channel = channel
        self.lines = tuple(lines)
        self.status = status
        self.placed_at = placed_at
        self.customer_email = customer_email
        self.payment_method = payment_method
        self.checkout = checkout
        self.hold_expires_at = hold_expires_at
        self.payment_id = payment_id
        self.cancellation_reason = cancellation_reason
        self.refund_reason = refund_reason
        self.closed_at = closed_at

    @classmethod
    def place(
        cls,
        *,
        order_id: OrderId,
        channel: SalesChannel,
        lines: Sequence[OrderLine],
        placed_at: datetime,
        customer_email: str | None = None,
    ) -> Order:
        order = cls(
            order_id=order_id,
            channel=channel,
            lines=lines,
            status=OrderStatus.PLACED,
            placed_at=placed_at,
            customer_email=customer_email,
        )
        order._record(OrderPlaced(order_id=order_id, channel=channel, total=order.total))
        return order

    @property
    def total(self) -> Money:
        total = Money.zero(self.lines[0].unit_price.currency)
        for line in self.lines:
            total += line.subtotal
        return total

    # --- Mostrador -------------------------------------------------------

    def complete_counter_sale(self, payment_method: PaymentMethod, at: datetime) -> None:
        self._require_channel(SalesChannel.COUNTER)
        self._require_status(OrderStatus.PLACED)
        self.status = OrderStatus.COMPLETED
        self.payment_method = payment_method
        self.closed_at = at
        self._record(
            CounterSaleCompleted(
                order_id=self.order_id, total=self.total, payment_method=payment_method
            )
        )

    # --- Online ----------------------------------------------------------

    def await_payment(
        self,
        method: PaymentMethod,
        hold_expires_at: datetime,
        checkout: CheckoutSession | None = None,
    ) -> None:
        """El stock quedó apartado hasta `hold_expires_at` esperando el pago.

        Mercado Pago necesita la sesión de pago creada en la pasarela; la
        transferencia no (el comprador recibe los datos bancarios).
        """
        self._require_channel(SalesChannel.ONLINE)
        self._require_status(OrderStatus.PLACED)
        if method not in ONLINE_PAYMENT_METHODS:
            raise InvalidOrderError(f"Medio de pago no disponible para compras online: {method}")
        if method is PaymentMethod.MERCADOPAGO and checkout is None:
            raise InvalidOrderError("Un pago con Mercado Pago necesita la sesión de pago")
        self.status = OrderStatus.AWAITING_PAYMENT
        self.payment_method = method
        self.checkout = checkout
        self.hold_expires_at = hold_expires_at
        self._record(
            OrderAwaitingPayment(
                order_id=self.order_id,
                payment_method=method,
                checkout_id=checkout.checkout_id if checkout else None,
            )
        )

    def mark_paid(self, payment_id: str, amount: Money, at: datetime) -> None:
        """Pago aprobado por la pasarela (Mercado Pago)."""
        if self.status is OrderStatus.PAID and self.payment_id == payment_id:
            return  # notificación repetida del mismo pago
        self._require_status(OrderStatus.AWAITING_PAYMENT)
        self._require_payment_method(PaymentMethod.MERCADOPAGO)
        if amount != self.total:
            raise PaymentAmountMismatchError(f"Se cobró {amount} y la orden suma {self.total}")
        self.status = OrderStatus.PAID
        self.payment_id = payment_id
        self.closed_at = at
        self._record(OrderPaid(order_id=self.order_id, payment_id=payment_id, total=self.total))

    def confirm_transfer(self, bank_reference: str | None, at: datetime) -> None:
        """El personal verificó que la transferencia se acreditó en la cuenta."""
        if self.status is OrderStatus.PAID and self.payment_method is PaymentMethod.BANK_TRANSFER:
            return  # confirmación repetida
        self._require_status(OrderStatus.AWAITING_PAYMENT)
        self._require_payment_method(PaymentMethod.BANK_TRANSFER)
        self.status = OrderStatus.PAID
        self.payment_id = (
            bank_reference.strip() if bank_reference and bank_reference.strip() else None
        )
        self.closed_at = at
        self._record(
            OrderPaid(order_id=self.order_id, payment_id=self.payment_id, total=self.total)
        )

    def require_refund(self, payment_id: str, reason: RefundReason, at: datetime) -> None:
        """Se cobró un pago que no se puede convertir en entrega."""
        if self.status is OrderStatus.REFUND_REQUIRED and self.payment_id == payment_id:
            return
        if self.status in {OrderStatus.COMPLETED, OrderStatus.PAID}:
            raise InvalidOrderStateError(
                f"La orden {self.order_id} ya está cerrada como {self.status}"
            )
        self.status = OrderStatus.REFUND_REQUIRED
        self.payment_id = payment_id
        self.refund_reason = reason
        self.closed_at = at
        self._record(
            OrderRefundRequired(order_id=self.order_id, payment_id=payment_id, reason=reason)
        )

    # --- Común -----------------------------------------------------------

    def cancel(self, reason: CancellationReason, at: datetime) -> None:
        if self.status is OrderStatus.CANCELLED:
            return
        if self.status not in _CANCELLABLE:
            raise InvalidOrderStateError(
                f"No se puede cancelar la orden {self.order_id} en estado {self.status}"
            )
        self.status = OrderStatus.CANCELLED
        self.cancellation_reason = reason
        self.closed_at = at
        self._record(OrderCancelled(order_id=self.order_id, reason=reason))

    def _require_channel(self, channel: SalesChannel) -> None:
        if self.channel is not channel:
            raise InvalidOrderStateError(
                f"Operación de canal {channel} sobre una orden {self.channel}"
            )

    def _require_payment_method(self, method: PaymentMethod) -> None:
        if self.payment_method is not method:
            raise InvalidOrderStateError(
                f"La orden {self.order_id} se paga con {self.payment_method}, no con {method}"
            )

    def _require_status(self, status: OrderStatus) -> None:
        if self.status is not status:
            raise InvalidOrderStateError(
                f"La orden {self.order_id} está {self.status}, se esperaba {status}"
            )
