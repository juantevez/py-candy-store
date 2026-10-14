from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.application.dto import StockLine
from candy_store.inventory.application.use_cases.deduct_stock_fefo import (
    DeductStockFefoCommand,
    DeductStockFefoUseCase,
)
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.inventory.infrastructure.persistence.models import StockReservationRow
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.shared.application.unit_of_work import ConcurrencyError
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.outbox import OutboxMessage
from tests.fakes import FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

pytestmark = pytest.mark.integration

LOCATION = StockLocation.STORE_FRONT
type SessionFactory = async_sessionmaker[AsyncSession]


async def receive(
    factory: SessionFactory, product_id: ProductId, *, quantity: int, expires_in_days: int
) -> BatchId:
    return await ReceiveStockBatchUseCase(
        SqlAlchemyInventoryUnitOfWork(factory), FixedClock(NOW)
    ).execute(
        ReceiveStockBatchCommand(
            product_id=product_id,
            location=LOCATION,
            lot_number=f"L-{expires_in_days}",
            expiration=TODAY + timedelta(days=expires_in_days),
            quantity=quantity,
        )
    )


async def load(factory: SessionFactory, product_id: ProductId) -> InventoryItem:
    async with SqlAlchemyInventoryUnitOfWork(factory) as uow:
        item = await uow.items.get(product_id, LOCATION)
    assert item is not None
    return item


async def outbox(factory: SessionFactory) -> list[OutboxMessage]:
    async with factory() as session:
        stmt = select(OutboxMessage).order_by(OutboxMessage.occurred_at)
        return list((await session.scalars(stmt)).all())


async def test_received_batch_round_trips_and_event_lands_in_outbox(
    session_factory: SessionFactory,
) -> None:
    product_id = ProductId.new()

    batch_id = await receive(session_factory, product_id, quantity=24, expires_in_days=30)

    item = await load(session_factory, product_id)
    [batch] = item.batches
    assert batch.batch_id == batch_id
    assert batch.lot_number == LotNumber("L-30")
    assert batch.expiration == ExpirationDate(TODAY + timedelta(days=30))
    assert batch.quantity_on_hand == 24
    assert batch.received_at == NOW
    assert item.version == 1

    [message] = await outbox(session_factory)
    assert message.context == "inventory"
    assert message.event_type == "StockBatchReceived"
    assert message.published_at is None
    assert message.payload["product_id"] == str(product_id)
    assert message.payload["batch_id"] == str(batch_id)
    assert message.payload["lot_number"] == "L-30"
    assert message.payload["location"] == "store_front"
    assert message.payload["quantity"] == 24


async def test_fefo_deduction_is_persisted(session_factory: SessionFactory) -> None:
    product_id = ProductId.new()
    later = await receive(session_factory, product_id, quantity=10, expires_in_days=60)
    sooner = await receive(session_factory, product_id, quantity=3, expires_in_days=5)

    await DeductStockFefoUseCase(
        SqlAlchemyInventoryUnitOfWork(session_factory), FixedClock(NOW)
    ).execute(
        DeductStockFefoCommand(
            reference=StockReference("T-1"),
            location=LOCATION,
            lines=[StockLine(product_id=product_id, quantity=5)],
        )
    )

    item = await load(session_factory, product_id)
    on_hand = {b.batch_id: b.quantity_on_hand for b in item.batches}
    assert on_hand == {sooner: 0, later: 8}
    assert item.version == 3
    deducted = (await outbox(session_factory))[-1]
    assert deducted.event_type == "StockDeducted"
    assert deducted.payload["reference"] == "T-1"
    assert deducted.payload["allocations"] == [
        {"batch_id": str(sooner), "lot_number": "L-5", "expiration": "2026-10-19", "quantity": 3},
        {"batch_id": str(later), "lot_number": "L-60", "expiration": "2026-12-13", "quantity": 2},
    ]


async def test_reservations_are_persisted_and_removed_on_confirm(
    session_factory: SessionFactory,
) -> None:
    product_id = ProductId.new()
    await receive(session_factory, product_id, quantity=10, expires_in_days=30)
    reference = StockReference("ORD-1")

    async with SqlAlchemyInventoryUnitOfWork(session_factory) as uow:
        item = await uow.items.get(product_id, LOCATION)
        assert item is not None
        item.reserve(
            reference=reference,
            quantity=4,
            expires_at=NOW + timedelta(minutes=15),
            today=TODAY,
        )
        await uow.commit()

    reloaded = await load(session_factory, product_id)
    assert reloaded.reserved_quantity() == 4
    assert reloaded.available_quantity(TODAY) == 6

    async with SqlAlchemyInventoryUnitOfWork(session_factory) as uow:
        item = await uow.items.get(product_id, LOCATION)
        assert item is not None
        item.confirm_reservation(reference, TODAY)
        await uow.commit()

    reloaded = await load(session_factory, product_id)
    assert reloaded.reserved_quantity() == 0
    assert reloaded.quantity_on_hand() == 6
    async with session_factory() as session:
        assert (await session.scalars(select(StockReservationRow))).all() == []


async def test_concurrent_modification_is_rejected(session_factory: SessionFactory) -> None:
    """Venta POS y web sobre el mismo producto a la vez: solo una puede confirmar."""
    product_id = ProductId.new()
    await receive(session_factory, product_id, quantity=10, expires_in_days=30)

    async with (
        SqlAlchemyInventoryUnitOfWork(session_factory) as pos,
        SqlAlchemyInventoryUnitOfWork(session_factory) as web,
    ):
        pos_item = await pos.items.get(product_id, LOCATION)
        web_item = await web.items.get(product_id, LOCATION)
        assert pos_item is not None
        assert web_item is not None
        pos_item.deduct(7, TODAY, reference=StockReference("POS-1"))
        web_item.deduct(7, TODAY, reference=StockReference("POS-2"))

        await pos.commit()
        with pytest.raises(ConcurrencyError):
            await web.commit()

    item = await load(session_factory, product_id)
    assert item.quantity_on_hand() == 3
    assert [m.event_type for m in await outbox(session_factory)] == [
        "StockBatchReceived",
        "StockDeducted",
    ]


async def test_concurrent_creation_of_same_item_is_rejected(
    session_factory: SessionFactory,
) -> None:
    product_id = ProductId.new()

    async with (
        SqlAlchemyInventoryUnitOfWork(session_factory) as first,
        SqlAlchemyInventoryUnitOfWork(session_factory) as second,
    ):
        for uow in (first, second):
            assert await uow.items.get(product_id, LOCATION) is None
            item = InventoryItem(product_id=product_id, location=LOCATION)
            await uow.items.add(item)
            item.receive_batch(
                batch_id=BatchId.new(),
                lot_number=LotNumber("X"),
                expiration=ExpirationDate(TODAY + timedelta(days=10)),
                quantity=1,
                received_at=NOW,
                today=TODAY,
            )

        await first.commit()
        with pytest.raises(ConcurrencyError):
            await second.commit()


async def test_changes_without_commit_are_discarded(session_factory: SessionFactory) -> None:
    product_id = ProductId.new()
    await receive(session_factory, product_id, quantity=10, expires_in_days=30)

    async with SqlAlchemyInventoryUnitOfWork(session_factory) as uow:
        item = await uow.items.get(product_id, LOCATION)
        assert item is not None
        item.deduct(10, TODAY, reference=StockReference("POS-1"))

    assert (await load(session_factory, product_id)).quantity_on_hand() == 10
    assert len(await outbox(session_factory)) == 1
