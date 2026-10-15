from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.use_cases._orders import find_order
from candy_store.sales.domain.errors import OrderNotFoundError
from candy_store.sales.domain.model import Order
from candy_store.sales.domain.value_objects import (
    CancellationReason,
    OrderId,
    OrderLine,
    OrderStatus,
    PaymentMethod,
    RefundReason,
    SalesChannel,
)
from candy_store.shared.domain.value_objects import Money


@dataclass(frozen=True, slots=True)
class OrderView:
    order_id: OrderId
    channel: SalesChannel
    status: OrderStatus
    lines: tuple[OrderLine, ...]
    total: Money
    placed_at: datetime
    payment_method: PaymentMethod | None
    checkout_url: str | None
    hold_expires_at: datetime | None
    payment_id: str | None
    cancellation_reason: CancellationReason | None
    refund_reason: RefundReason | None

    @classmethod
    def of(cls, order: Order) -> OrderView:
        return cls(
            order_id=order.order_id,
            channel=order.channel,
            status=order.status,
            lines=order.lines,
            total=order.total,
            placed_at=order.placed_at,
            payment_method=order.payment_method,
            checkout_url=order.checkout.redirect_url if order.checkout else None,
            hold_expires_at=order.hold_expires_at,
            payment_id=order.payment_id,
            cancellation_reason=order.cancellation_reason,
            refund_reason=order.refund_reason,
        )


class GetOrderUseCase:
    def __init__(self, uow: SalesUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, order_id: OrderId) -> OrderView:
        order = await find_order(self._uow, order_id)
        if order is None:
            raise OrderNotFoundError(f"No existe la orden {order_id}")
        return OrderView.of(order)


class ListRecentOrdersUseCase:
    def __init__(self, uow: SalesUnitOfWork) -> None:
        self._uow = uow

    async def execute(
        self, *, channel: SalesChannel | None = None, limit: int = 20
    ) -> list[OrderView]:
        async with self._uow as uow:
            orders = await uow.orders.list_recent(channel=channel, limit=limit)
        return [OrderView.of(order) for order in orders]
