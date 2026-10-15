from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.sales.application.ports.repository import OrderRepository
from candy_store.sales.domain.model import Order
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
from candy_store.sales.infrastructure.persistence.models import OrderLineRow, OrderRow
from candy_store.shared.domain.value_objects import Money, ProductId


class SqlAlchemyOrderRepository(OrderRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._seen: dict[OrderId, tuple[Order, OrderRow | None]] = {}

    async def get(self, order_id: OrderId) -> Order | None:
        if order_id in self._seen:
            return self._seen[order_id][0]
        row = await self._session.get(OrderRow, order_id.value)
        if row is None:
            return None
        order = _to_domain(row)
        self._seen[order_id] = (order, row)
        return order

    async def add(self, order: Order) -> None:
        self._seen[order.order_id] = (order, None)

    async def list_recent(self, *, channel: SalesChannel | None, limit: int) -> list[Order]:
        query = select(OrderRow).order_by(OrderRow.placed_at.desc()).limit(limit)
        if channel is not None:
            query = query.where(OrderRow.channel == channel.value)
        rows = await self._session.scalars(query)
        return [_to_domain(row) for row in rows]

    @property
    def seen(self) -> list[Order]:
        return [order for order, _ in self._seen.values()]

    def persist_seen(self) -> None:
        for order_id, (order, row) in self._seen.items():
            if row is None:
                row = _new_row(order)
                self._session.add(row)
                self._seen[order_id] = (order, row)
            row.status = order.status.value
            row.customer_email = order.customer_email
            row.payment_method = order.payment_method.value if order.payment_method else None
            row.checkout_id = order.checkout.checkout_id if order.checkout else None
            row.checkout_url = order.checkout.redirect_url if order.checkout else None
            row.hold_expires_at = order.hold_expires_at
            row.payment_id = order.payment_id
            row.cancellation_reason = (
                order.cancellation_reason.value if order.cancellation_reason else None
            )
            row.refund_reason = order.refund_reason.value if order.refund_reason else None
            row.closed_at = order.closed_at
            row.version = order.version + 1
            order.version = row.version


def _new_row(order: Order) -> OrderRow:
    # Las líneas son inmutables: se escriben una sola vez, al crear la orden.
    return OrderRow(
        id=order.order_id.value,
        channel=order.channel.value,
        placed_at=order.placed_at,
        currency=order.total.currency,
        total_amount=order.total.amount,
        version=0,
        lines=[
            OrderLineRow(
                line_number=number,
                product_id=line.product_id.value,
                description=line.description,
                unit_price=line.unit_price.amount,
                quantity=line.quantity,
            )
            for number, line in enumerate(order.lines, start=1)
        ],
    )


def _to_domain(row: OrderRow) -> Order:
    return Order(
        order_id=OrderId(row.id),
        channel=SalesChannel(row.channel),
        lines=[
            OrderLine(
                product_id=ProductId(line.product_id),
                description=line.description,
                unit_price=Money(line.unit_price, row.currency),
                quantity=line.quantity,
            )
            for line in row.lines
        ],
        status=OrderStatus(row.status),
        placed_at=row.placed_at,
        customer_email=row.customer_email,
        payment_method=PaymentMethod(row.payment_method) if row.payment_method else None,
        checkout=(
            CheckoutSession(checkout_id=row.checkout_id, redirect_url=row.checkout_url)
            if row.checkout_id and row.checkout_url
            else None
        ),
        hold_expires_at=row.hold_expires_at,
        payment_id=row.payment_id,
        cancellation_reason=(
            CancellationReason(row.cancellation_reason) if row.cancellation_reason else None
        ),
        refund_reason=RefundReason(row.refund_reason) if row.refund_reason else None,
        closed_at=row.closed_at,
        version=row.version,
    )
