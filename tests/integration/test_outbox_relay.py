from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.event_bus import EventBus
from candy_store.shared.infrastructure.outbox import OutboxMessage, OutboxRelay
from tests.fakes import FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

pytestmark = pytest.mark.integration


async def _receive_some_stock(factory: async_sessionmaker[AsyncSession], times: int) -> None:
    use_case = ReceiveStockBatchUseCase(SqlAlchemyInventoryUnitOfWork(factory), FixedClock(NOW))
    for i in range(times):
        await use_case.execute(
            ReceiveStockBatchCommand(
                product_id=ProductId.new(),
                location=StockLocation.WAREHOUSE,
                lot_number=f"L{i}",
                expiration=TODAY + timedelta(days=30),
                quantity=1,
            )
        )


async def test_relay_publishes_pending_messages_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _receive_some_stock(session_factory, times=3)
    received: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        received.append(payload)

    bus = EventBus()
    bus.subscribe("StockBatchReceived", handler)
    relay = OutboxRelay(session_factory, bus, batch_size=2)

    assert await relay.publish_pending() == 2
    assert await relay.publish_pending() == 1
    assert await relay.publish_pending() == 0
    assert [p["lot_number"] for p in received] == ["L0", "L1", "L2"]

    async with session_factory() as session:
        messages = (await session.scalars(select(OutboxMessage))).all()
    assert all(m.published_at is not None for m in messages)


async def test_failing_handler_leaves_message_pending_for_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _receive_some_stock(session_factory, times=1)

    async def broken_handler(payload: dict[str, Any]) -> None:
        raise RuntimeError("subscriber caído")

    bus = EventBus()
    bus.subscribe("StockBatchReceived", broken_handler)

    with pytest.raises(RuntimeError):
        await OutboxRelay(session_factory, bus).publish_pending()

    async with session_factory() as session:
        [message] = (await session.scalars(select(OutboxMessage))).all()
    assert message.published_at is None
