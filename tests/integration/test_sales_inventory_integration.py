"""Sales usando inventory a través de su StockGateway, con la base de datos real."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.application.use_cases.get_stock_summary import GetStockSummaryUseCase
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
)
from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.inventory.public import InventoryFacade
from candy_store.sales.application.ports.stock import (
    ItemQuantity,
    OutOfStockError,
    StockHoldLostError,
)
from candy_store.sales.domain.value_objects import OrderId
from candy_store.sales.infrastructure.inventory.stock_gateway import InProcessStockGateway
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.outbox import OutboxMessage
from tests.fakes import FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

pytestmark = pytest.mark.integration

type SessionFactory = async_sessionmaker[AsyncSession]
TTL = timedelta(minutes=15)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def gateway(session_factory: SessionFactory, clock: FixedClock) -> InProcessStockGateway:
    facade = InventoryFacade(
        uow_factory=lambda: SqlAlchemyInventoryUnitOfWork(session_factory),
        clock=clock,
        reservation_ttl=TTL,
    )
    return InProcessStockGateway(facade)


async def stock(
    factory: SessionFactory, product_id: ProductId, quantity: int, location: StockLocation
) -> None:
    await ReceiveStockBatchUseCase(SqlAlchemyInventoryUnitOfWork(factory), FixedClock(NOW)).execute(
        ReceiveStockBatchCommand(
            product_id=product_id,
            location=location,
            lot_number="L1",
            expiration=TODAY + timedelta(days=30),
            quantity=quantity,
        )
    )


async def on_hand(factory: SessionFactory, product_id: ProductId, location: StockLocation) -> int:
    summary = await GetStockSummaryUseCase(
        SqlAlchemyInventoryUnitOfWork(factory), FixedClock(NOW)
    ).execute(product_id)
    return sum(loc.on_hand for loc in summary.locations if loc.location == location)


async def available(factory: SessionFactory, product_id: ProductId) -> int:
    summary = await GetStockSummaryUseCase(
        SqlAlchemyInventoryUnitOfWork(factory), FixedClock(NOW)
    ).execute(product_id)
    return summary.total_available


# --- Venta de mostrador -------------------------------------------------------


async def test_counter_sale_takes_store_front_stock_once(
    gateway: InProcessStockGateway, session_factory: SessionFactory
) -> None:
    alfajor, chicle = ProductId.new(), ProductId.new()
    await stock(session_factory, alfajor, 10, StockLocation.STORE_FRONT)
    await stock(session_factory, chicle, 10, StockLocation.STORE_FRONT)
    await stock(session_factory, alfajor, 50, StockLocation.WAREHOUSE)
    order_id = OrderId.new()
    items = [ItemQuantity(alfajor, 2), ItemQuantity(chicle, 3)]

    await gateway.take_for_counter_sale(order_id, items)
    await gateway.take_for_counter_sale(order_id, items)  # reintento del POS

    assert await on_hand(session_factory, alfajor, StockLocation.STORE_FRONT) == 8
    assert await on_hand(session_factory, chicle, StockLocation.STORE_FRONT) == 7
    assert await on_hand(session_factory, alfajor, StockLocation.WAREHOUSE) == 50
    async with session_factory() as session:
        deducted = (
            await session.scalars(
                select(OutboxMessage).where(OutboxMessage.event_type == "StockDeducted")
            )
        ).all()
    assert len(deducted) == 2  # una por producto, no por intento
    assert {m.payload["reference"] for m in deducted} == {str(order_id)}


async def test_counter_sale_without_stock_reports_all_items_and_moves_nothing(
    gateway: InProcessStockGateway, session_factory: SessionFactory
) -> None:
    alfajor, chicle, turron = ProductId.new(), ProductId.new(), ProductId.new()
    await stock(session_factory, alfajor, 10, StockLocation.STORE_FRONT)
    await stock(session_factory, chicle, 1, StockLocation.STORE_FRONT)

    with pytest.raises(OutOfStockError) as exc:
        await gateway.take_for_counter_sale(
            OrderId.new(),
            [ItemQuantity(alfajor, 2), ItemQuantity(chicle, 3), ItemQuantity(turron, 1)],
        )

    assert {(i.product_id, i.requested, i.available) for i in exc.value.items} == {
        (chicle, 3, 1),
        (turron, 1, 0),
    }
    assert await on_hand(session_factory, alfajor, StockLocation.STORE_FRONT) == 10


async def test_concurrent_counter_sales_cannot_oversell(
    gateway: InProcessStockGateway, session_factory: SessionFactory
) -> None:
    """Dos cajas venden las últimas unidades a la vez: una gana, la otra se entera."""
    product_id = ProductId.new()
    await stock(session_factory, product_id, 5, StockLocation.STORE_FRONT)

    results = await asyncio.gather(
        gateway.take_for_counter_sale(OrderId.new(), [ItemQuantity(product_id, 4)]),
        gateway.take_for_counter_sale(OrderId.new(), [ItemQuantity(product_id, 4)]),
        return_exceptions=True,
    )

    assert sorted(type(r).__name__ for r in results) == ["NoneType", "OutOfStockError"]
    assert await on_hand(session_factory, product_id, StockLocation.STORE_FRONT) == 1


# --- Orden online -------------------------------------------------------------


async def test_online_order_holds_then_commits_from_warehouse(
    gateway: InProcessStockGateway, session_factory: SessionFactory, clock: FixedClock
) -> None:
    product_id = ProductId.new()
    await stock(session_factory, product_id, 10, StockLocation.WAREHOUSE)
    order_id = OrderId.new()

    expires_at = await gateway.hold_for_online_order(order_id, [ItemQuantity(product_id, 4)])
    assert expires_at == NOW + TTL
    assert await available(session_factory, product_id) == 6

    await gateway.commit_online_order(order_id)

    assert await on_hand(session_factory, product_id, StockLocation.WAREHOUSE) == 6
    assert await available(session_factory, product_id) == 6


async def test_duplicate_payment_webhooks_in_parallel_commit_once(
    gateway: InProcessStockGateway, session_factory: SessionFactory
) -> None:
    product_id = ProductId.new()
    await stock(session_factory, product_id, 10, StockLocation.WAREHOUSE)
    order_id = OrderId.new()
    await gateway.hold_for_online_order(order_id, [ItemQuantity(product_id, 4)])

    await asyncio.gather(*(gateway.commit_online_order(order_id) for _ in range(3)))

    assert await on_hand(session_factory, product_id, StockLocation.WAREHOUSE) == 6


async def test_cancelled_online_order_returns_stock(
    gateway: InProcessStockGateway, session_factory: SessionFactory
) -> None:
    product_id = ProductId.new()
    await stock(session_factory, product_id, 10, StockLocation.WAREHOUSE)
    order_id = OrderId.new()
    await gateway.hold_for_online_order(order_id, [ItemQuantity(product_id, 10)])

    await gateway.cancel_online_order(order_id)
    await gateway.cancel_online_order(order_id)  # no falla si ya se canceló

    assert await available(session_factory, product_id) == 10


async def test_payment_after_hold_expired(
    gateway: InProcessStockGateway, session_factory: SessionFactory, clock: FixedClock
) -> None:
    product_id = ProductId.new()
    await stock(session_factory, product_id, 10, StockLocation.WAREHOUSE)
    order_id = OrderId.new()
    await gateway.hold_for_online_order(order_id, [ItemQuantity(product_id, 4)])

    clock.current = NOW + TTL + timedelta(seconds=1)
    await ReleaseExpiredReservationsUseCase(
        SqlAlchemyInventoryUnitOfWork(session_factory), clock
    ).execute()

    with pytest.raises(StockHoldLostError):
        await gateway.commit_online_order(order_id)
    await gateway.cancel_online_order(order_id)  # cancelar algo ya liberado no falla
    assert await available(session_factory, product_id) == 10
