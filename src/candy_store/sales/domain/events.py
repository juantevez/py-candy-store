from dataclasses import dataclass

from candy_store.sales.domain.value_objects import (
    CancellationReason,
    OrderId,
    PaymentMethod,
    RefundReason,
    SalesChannel,
)
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import Money


@dataclass(frozen=True, kw_only=True)
class OrderPlaced(DomainEvent):
    order_id: OrderId
    channel: SalesChannel
    total: Money


@dataclass(frozen=True, kw_only=True)
class CounterSaleCompleted(DomainEvent):
    order_id: OrderId
    total: Money
    payment_method: PaymentMethod


@dataclass(frozen=True, kw_only=True)
class OrderAwaitingPayment(DomainEvent):
    order_id: OrderId
    checkout_id: str


@dataclass(frozen=True, kw_only=True)
class OrderPaid(DomainEvent):
    order_id: OrderId
    payment_id: str
    total: Money


@dataclass(frozen=True, kw_only=True)
class OrderCancelled(DomainEvent):
    order_id: OrderId
    reason: CancellationReason


@dataclass(frozen=True, kw_only=True)
class OrderRefundRequired(DomainEvent):
    order_id: OrderId
    payment_id: str
    reason: RefundReason
