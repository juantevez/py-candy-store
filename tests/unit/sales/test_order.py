from datetime import UTC, datetime, timedelta

import pytest

from candy_store.sales.domain.errors import (
    EmptyOrderError,
    InvalidOrderError,
    InvalidOrderStateError,
    PaymentAmountMismatchError,
)
from candy_store.sales.domain.events import (
    CounterSaleCompleted,
    OrderCancelled,
    OrderPaid,
    OrderPlaced,
    OrderRefundRequired,
)
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
from candy_store.shared.domain.value_objects import Money, ProductId

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
SESSION = CheckoutSession(checkout_id="pref-1", redirect_url="https://mp.test/1")


def line(price: str = "1500", quantity: int = 2) -> OrderLine:
    return OrderLine(
        product_id=ProductId.new(),
        description="Alfajor triple",
        unit_price=Money.of(price),
        quantity=quantity,
    )


def place(channel: SalesChannel, *lines: OrderLine) -> Order:
    return Order.place(
        order_id=OrderId.new(),
        channel=channel,
        lines=lines or (line(),),
        placed_at=NOW,
    )


def awaiting_payment() -> Order:
    order = place(SalesChannel.ONLINE, line("1500", 2), line("800", 1))
    order.await_payment(SESSION, NOW + timedelta(minutes=15))
    order.pull_events()
    return order


def test_total_sums_frozen_line_prices() -> None:
    order = place(SalesChannel.COUNTER, line("1500.50", 2), line("800", 3))

    assert order.total == Money.of("5401.00")
    [event] = order.pull_events()
    assert isinstance(event, OrderPlaced)
    assert event.total == Money.of("5401.00")


def test_order_needs_lines() -> None:
    with pytest.raises(EmptyOrderError):
        Order.place(order_id=OrderId.new(), channel=SalesChannel.COUNTER, lines=[], placed_at=NOW)


def test_product_cannot_repeat_across_lines() -> None:
    repeated = line()
    with pytest.raises(InvalidOrderError):
        place(SalesChannel.COUNTER, repeated, repeated)


@pytest.mark.parametrize(("price", "quantity"), [("0", 1), ("100", 0), ("100", -1)])
def test_invalid_lines(price: str, quantity: int) -> None:
    with pytest.raises(InvalidOrderError):
        line(price, quantity)


def test_counter_sale_completion() -> None:
    order = place(SalesChannel.COUNTER)
    order.pull_events()

    order.complete_counter_sale(PaymentMethod.CASH, NOW)

    assert order.status is OrderStatus.COMPLETED
    assert order.payment_method is PaymentMethod.CASH
    [event] = order.pull_events()
    assert isinstance(event, CounterSaleCompleted)


def test_online_order_cannot_be_completed_at_the_counter() -> None:
    with pytest.raises(InvalidOrderStateError):
        place(SalesChannel.ONLINE).complete_counter_sale(PaymentMethod.CASH, NOW)


def test_counter_order_cannot_await_online_payment() -> None:
    with pytest.raises(InvalidOrderStateError):
        place(SalesChannel.COUNTER).await_payment(SESSION, NOW)


def test_online_payment_flow() -> None:
    order = awaiting_payment()

    order.mark_paid("pay-1", Money.of("3800"), NOW)
    order.mark_paid("pay-1", Money.of("3800"), NOW)  # notificación repetida

    assert order.status is OrderStatus.PAID
    assert order.payment_id == "pay-1"
    assert order.payment_method is PaymentMethod.MERCADOPAGO
    [event] = order.pull_events()
    assert isinstance(event, OrderPaid)


def test_payment_amount_must_match_total() -> None:
    with pytest.raises(PaymentAmountMismatchError):
        awaiting_payment().mark_paid("pay-1", Money.of("1"), NOW)


def test_cannot_pay_before_checkout_started() -> None:
    with pytest.raises(InvalidOrderStateError):
        place(SalesChannel.ONLINE).mark_paid("pay-1", Money.of("3000"), NOW)


def test_cancel_is_idempotent_and_closed_orders_cannot_be_cancelled() -> None:
    order = awaiting_payment()

    order.cancel(CancellationReason.HOLD_EXPIRED, NOW)
    order.cancel(CancellationReason.HOLD_EXPIRED, NOW)

    assert order.status is OrderStatus.CANCELLED
    assert len([e for e in order.pull_events() if isinstance(e, OrderCancelled)]) == 1

    paid = awaiting_payment()
    paid.mark_paid("pay-1", paid.total, NOW)
    with pytest.raises(InvalidOrderStateError):
        paid.cancel(CancellationReason.HOLD_EXPIRED, NOW)


def test_late_payment_on_cancelled_order_requires_refund() -> None:
    order = awaiting_payment()
    order.cancel(CancellationReason.HOLD_EXPIRED, NOW)
    order.pull_events()

    order.require_refund("pay-9", RefundReason.ORDER_CANCELLED, NOW)

    assert order.status is OrderStatus.REFUND_REQUIRED
    assert order.refund_reason is RefundReason.ORDER_CANCELLED
    [event] = order.pull_events()
    assert isinstance(event, OrderRefundRequired)


def test_paid_order_cannot_be_flagged_for_refund() -> None:
    order = awaiting_payment()
    order.mark_paid("pay-1", order.total, NOW)

    with pytest.raises(InvalidOrderStateError):
        order.require_refund("pay-2", RefundReason.ORDER_CANCELLED, NOW)
