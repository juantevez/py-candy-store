from datetime import UTC, datetime, timedelta

import pytest

from candy_store.sales.application.ports.payment_gateway import (
    PaymentGatewayError,
    PaymentStatus,
)
from candy_store.sales.application.ports.product_catalog import CatalogItem
from candy_store.sales.application.ports.stock import ItemQuantity, OutOfStockError
from candy_store.sales.application.use_cases.cancel_expired_order import (
    CancelExpiredOrderUseCase,
)
from candy_store.sales.application.use_cases.checkout_online_cart import (
    CheckoutOnlineCartCommand,
    CheckoutOnlineCartUseCase,
)
from candy_store.sales.application.use_cases.get_order import GetOrderUseCase
from candy_store.sales.application.use_cases.handle_payment_webhook import (
    HandlePaymentNotificationUseCase,
    PaymentNotificationOutcome,
)
from candy_store.sales.application.use_cases.process_pos_sale import (
    ProcessPosSaleCommand,
    ProcessPosSaleUseCase,
)
from candy_store.sales.domain.errors import (
    EmptyOrderError,
    InvalidOrderStateError,
    ProductNotSellableError,
)
from candy_store.sales.domain.value_objects import (
    CancellationReason,
    OrderId,
    OrderStatus,
    PaymentMethod,
    RefundReason,
)
from candy_store.shared.domain.value_objects import Money, ProductId
from tests.fakes import (
    HOLD_TTL,
    FakePaymentGateway,
    FakeProductCatalog,
    FakeSalesUnitOfWork,
    FakeStockGateway,
    FixedClock,
)

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
ALFAJOR = ProductId.new()
CHICLE = ProductId.new()
DISCONTINUED = ProductId.new()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def uow() -> FakeSalesUnitOfWork:
    return FakeSalesUnitOfWork()


@pytest.fixture
def stock(clock: FixedClock) -> FakeStockGateway:
    return FakeStockGateway({ALFAJOR: 10, CHICLE: 5}, clock)


@pytest.fixture
def payments() -> FakePaymentGateway:
    return FakePaymentGateway()


@pytest.fixture
def catalog() -> FakeProductCatalog:
    return FakeProductCatalog(
        CatalogItem(ALFAJOR, "Alfajor triple", Money.of("1500"), sellable=True),
        CatalogItem(CHICLE, "Chicle tutti frutti", Money.of("200"), sellable=True),
        CatalogItem(DISCONTINUED, "Turrón discontinuado", Money.of("900"), sellable=False),
    )


@pytest.fixture
def pos(
    uow: FakeSalesUnitOfWork,
    stock: FakeStockGateway,
    catalog: FakeProductCatalog,
    clock: FixedClock,
) -> ProcessPosSaleUseCase:
    return ProcessPosSaleUseCase(uow, stock, catalog, clock)


@pytest.fixture
def checkout(
    uow: FakeSalesUnitOfWork,
    stock: FakeStockGateway,
    catalog: FakeProductCatalog,
    payments: FakePaymentGateway,
    clock: FixedClock,
) -> CheckoutOnlineCartUseCase:
    return CheckoutOnlineCartUseCase(uow, stock, catalog, payments, clock)


@pytest.fixture
def webhook(
    uow: FakeSalesUnitOfWork,
    stock: FakeStockGateway,
    payments: FakePaymentGateway,
    clock: FixedClock,
) -> HandlePaymentNotificationUseCase:
    return HandlePaymentNotificationUseCase(uow, stock, payments, clock)


def counter_sale(order_id: OrderId, *items: tuple[ProductId, int]) -> ProcessPosSaleCommand:
    return ProcessPosSaleCommand(
        order_id=order_id,
        items=[ItemQuantity(pid, qty) for pid, qty in items],
        payment_method=PaymentMethod.CASH,
    )


def cart(order_id: OrderId, *items: tuple[ProductId, int]) -> CheckoutOnlineCartCommand:
    return CheckoutOnlineCartCommand(
        order_id=order_id,
        items=[ItemQuantity(pid, qty) for pid, qty in items],
        customer_email="cliente@example.com",
    )


# --- Venta de mostrador ---------------------------------------------------------


async def test_counter_sale_uses_catalog_prices_and_takes_stock(
    pos: ProcessPosSaleUseCase, stock: FakeStockGateway
) -> None:
    view = await pos.execute(counter_sale(OrderId.new(), (ALFAJOR, 2), (CHICLE, 3), (ALFAJOR, 1)))

    assert view.status is OrderStatus.COMPLETED
    assert view.total == Money.of("5100")  # 3 x 1500 + 3 x 200
    assert {line.description for line in view.lines} == {"Alfajor triple", "Chicle tutti frutti"}
    assert stock.available == {ALFAJOR: 7, CHICLE: 2}


async def test_counter_sale_is_idempotent(
    pos: ProcessPosSaleUseCase, stock: FakeStockGateway
) -> None:
    order_id = OrderId.new()

    first = await pos.execute(counter_sale(order_id, (ALFAJOR, 2)))
    retry = await pos.execute(counter_sale(order_id, (ALFAJOR, 2)))

    assert retry == first
    assert stock.available[ALFAJOR] == 8


async def test_counter_sale_without_stock_cancels_the_order(
    pos: ProcessPosSaleUseCase, uow: FakeSalesUnitOfWork
) -> None:
    order_id = OrderId.new()

    with pytest.raises(OutOfStockError):
        await pos.execute(counter_sale(order_id, (CHICLE, 6)))

    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.status is OrderStatus.CANCELLED
    assert view.cancellation_reason is CancellationReason.OUT_OF_STOCK


async def test_counter_sale_resumes_after_a_crash_between_steps(
    pos: ProcessPosSaleUseCase,
    uow: FakeSalesUnitOfWork,
    stock: FakeStockGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Se cae el proceso después de descontar stock y antes de cerrar la orden:
    el reintento de la caja termina la venta sin descontar dos veces."""
    order_id = OrderId.new()
    original = stock.take_for_counter_sale

    async def take_then_crash(oid: OrderId, items: list[ItemQuantity]) -> None:
        await original(oid, items)
        raise ConnectionError("se cortó la red")

    monkeypatch.setattr(stock, "take_for_counter_sale", take_then_crash)
    with pytest.raises(ConnectionError):
        await pos.execute(counter_sale(order_id, (ALFAJOR, 2)))
    assert (await GetOrderUseCase(uow).execute(order_id)).status is OrderStatus.PLACED

    monkeypatch.setattr(stock, "take_for_counter_sale", original)
    view = await pos.execute(counter_sale(order_id, (ALFAJOR, 2)))

    assert view.status is OrderStatus.COMPLETED
    assert stock.available[ALFAJOR] == 8


async def test_unknown_or_inactive_products_cannot_be_sold(pos: ProcessPosSaleUseCase) -> None:
    unknown = ProductId.new()
    with pytest.raises(ProductNotSellableError) as exc:
        await pos.execute(
            counter_sale(OrderId.new(), (ALFAJOR, 1), (DISCONTINUED, 1), (unknown, 1))
        )
    assert set(exc.value.product_ids) == {str(DISCONTINUED), str(unknown)}


async def test_empty_sale_is_rejected(pos: ProcessPosSaleUseCase) -> None:
    with pytest.raises(EmptyOrderError):
        await pos.execute(counter_sale(OrderId.new()))


# --- Checkout online ------------------------------------------------------------


async def test_checkout_holds_stock_and_creates_payment_session(
    checkout: CheckoutOnlineCartUseCase, stock: FakeStockGateway, payments: FakePaymentGateway
) -> None:
    order_id = OrderId.new()

    result = await checkout.execute(cart(order_id, (ALFAJOR, 2), (CHICLE, 1)))

    assert result.redirect_url == f"https://mp.test/checkout/{order_id}"
    assert result.total == Money.of("3200")
    assert result.expires_at == NOW + HOLD_TTL
    assert order_id in stock.holds
    [request] = payments.checkouts
    assert request.expires_at == result.expires_at
    assert request.payer_email == "cliente@example.com"
    assert {(i.title, i.quantity, i.unit_price) for i in request.items} == {
        ("Alfajor triple", 2, Money.of("1500")),
        ("Chicle tutti frutti", 1, Money.of("200")),
    }


async def test_checkout_retry_returns_same_session(
    checkout: CheckoutOnlineCartUseCase, payments: FakePaymentGateway
) -> None:
    order_id = OrderId.new()

    first = await checkout.execute(cart(order_id, (ALFAJOR, 2)))
    retry = await checkout.execute(cart(order_id, (ALFAJOR, 2)))

    assert retry == first
    assert len(payments.checkouts) == 1


async def test_checkout_without_stock(
    checkout: CheckoutOnlineCartUseCase, uow: FakeSalesUnitOfWork, payments: FakePaymentGateway
) -> None:
    order_id = OrderId.new()

    with pytest.raises(OutOfStockError):
        await checkout.execute(cart(order_id, (CHICLE, 9)))

    assert (await GetOrderUseCase(uow).execute(order_id)).status is OrderStatus.CANCELLED
    assert payments.checkouts == []


async def test_gateway_failure_releases_stock_and_cancels(
    checkout: CheckoutOnlineCartUseCase,
    uow: FakeSalesUnitOfWork,
    stock: FakeStockGateway,
    payments: FakePaymentGateway,
) -> None:
    payments.fail_checkout = True
    order_id = OrderId.new()

    with pytest.raises(PaymentGatewayError):
        await checkout.execute(cart(order_id, (ALFAJOR, 2)))

    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.cancellation_reason is CancellationReason.CHECKOUT_FAILED
    assert stock.available[ALFAJOR] == 10


async def test_counter_order_id_cannot_be_reused_for_checkout(
    pos: ProcessPosSaleUseCase, checkout: CheckoutOnlineCartUseCase
) -> None:
    order_id = OrderId.new()
    await pos.execute(counter_sale(order_id, (ALFAJOR, 1)))

    with pytest.raises(InvalidOrderStateError):
        await checkout.execute(cart(order_id, (ALFAJOR, 1)))


# --- Webhook de pago --------------------------------------------------------------


async def start_checkout(checkout: CheckoutOnlineCartUseCase) -> OrderId:
    order_id = OrderId.new()
    await checkout.execute(cart(order_id, (ALFAJOR, 2)))  # total 3000
    return order_id


async def test_approved_payment_commits_stock_and_marks_paid(
    checkout: CheckoutOnlineCartUseCase,
    webhook: HandlePaymentNotificationUseCase,
    payments: FakePaymentGateway,
    stock: FakeStockGateway,
    uow: FakeSalesUnitOfWork,
) -> None:
    order_id = await start_checkout(checkout)
    payment_id = payments.register("1001", order_id, Money.of("3000"))

    assert await webhook.execute(payment_id) is PaymentNotificationOutcome.PAID
    assert await webhook.execute(payment_id) is PaymentNotificationOutcome.ALREADY_PROCESSED

    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.status is OrderStatus.PAID
    assert view.payment_id == "1001"
    assert order_id in stock.taken


@pytest.mark.parametrize("status", [PaymentStatus.PENDING, PaymentStatus.REJECTED])
async def test_not_approved_payment_leaves_order_waiting(
    checkout: CheckoutOnlineCartUseCase,
    webhook: HandlePaymentNotificationUseCase,
    payments: FakePaymentGateway,
    uow: FakeSalesUnitOfWork,
    status: PaymentStatus,
) -> None:
    order_id = await start_checkout(checkout)
    payments.register("1001", order_id, Money.of("3000"), status)

    assert await webhook.execute("1001") is PaymentNotificationOutcome.NOT_APPROVED
    assert (await GetOrderUseCase(uow).execute(order_id)).status is OrderStatus.AWAITING_PAYMENT


async def test_payment_after_hold_expired_requires_refund(
    checkout: CheckoutOnlineCartUseCase,
    webhook: HandlePaymentNotificationUseCase,
    payments: FakePaymentGateway,
    stock: FakeStockGateway,
    uow: FakeSalesUnitOfWork,
) -> None:
    order_id = await start_checkout(checkout)
    stock.expire(order_id)  # inventory liberó la reserva, sales aún no se enteró
    payments.register("1001", order_id, Money.of("3000"))

    assert await webhook.execute("1001") is PaymentNotificationOutcome.REFUND_REQUIRED
    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.status is OrderStatus.REFUND_REQUIRED
    assert view.refund_reason is RefundReason.STOCK_UNAVAILABLE


async def test_payment_on_cancelled_order_requires_refund(
    checkout: CheckoutOnlineCartUseCase,
    webhook: HandlePaymentNotificationUseCase,
    payments: FakePaymentGateway,
    uow: FakeSalesUnitOfWork,
    clock: FixedClock,
) -> None:
    order_id = await start_checkout(checkout)
    await CancelExpiredOrderUseCase(uow, clock).execute(order_id)
    payments.register("1001", order_id, Money.of("3000"))

    assert await webhook.execute("1001") is PaymentNotificationOutcome.REFUND_REQUIRED
    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.refund_reason is RefundReason.ORDER_CANCELLED


async def test_amount_mismatch_requires_refund_and_releases_stock(
    checkout: CheckoutOnlineCartUseCase,
    webhook: HandlePaymentNotificationUseCase,
    payments: FakePaymentGateway,
    stock: FakeStockGateway,
    uow: FakeSalesUnitOfWork,
) -> None:
    order_id = await start_checkout(checkout)
    payments.register("1001", order_id, Money.of("1"))

    assert await webhook.execute("1001") is PaymentNotificationOutcome.REFUND_REQUIRED
    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.refund_reason is RefundReason.AMOUNT_MISMATCH
    assert stock.available[ALFAJOR] == 10


async def test_payments_unrelated_to_our_orders_are_ignored(
    webhook: HandlePaymentNotificationUseCase, payments: FakePaymentGateway
) -> None:
    payments.register("1", None, Money.of("10"))
    payments.register("2", OrderId.new(), Money.of("10"))

    assert await webhook.execute("1") is PaymentNotificationOutcome.IGNORED
    assert await webhook.execute("2") is PaymentNotificationOutcome.IGNORED


# --- Vencimiento de la reserva ------------------------------------------------------


async def test_expired_hold_cancels_awaiting_order_once(
    checkout: CheckoutOnlineCartUseCase, uow: FakeSalesUnitOfWork, clock: FixedClock
) -> None:
    order_id = await start_checkout(checkout)
    cancel = CancelExpiredOrderUseCase(uow, clock)

    assert await cancel.execute(order_id) is True
    assert await cancel.execute(order_id) is False  # otro producto de la misma orden

    view = await GetOrderUseCase(uow).execute(order_id)
    assert view.cancellation_reason is CancellationReason.HOLD_EXPIRED


async def test_expired_hold_does_not_touch_paid_order(
    checkout: CheckoutOnlineCartUseCase,
    webhook: HandlePaymentNotificationUseCase,
    payments: FakePaymentGateway,
    uow: FakeSalesUnitOfWork,
    clock: FixedClock,
) -> None:
    order_id = await start_checkout(checkout)
    await webhook.execute(payments.register("1001", order_id, Money.of("3000")))
    clock.current = NOW + timedelta(hours=1)

    assert await CancelExpiredOrderUseCase(uow, clock).execute(order_id) is False
    assert (await GetOrderUseCase(uow).execute(order_id)).status is OrderStatus.PAID
