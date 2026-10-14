from datetime import timedelta

import pytest

from candy_store.inventory.application.use_cases.deduct_stock_fefo import (
    DeductStockFefoCommand,
    DeductStockFefoUseCase,
)
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.application.use_cases.register_loss_adjustment import (
    RegisterAuditCountCommand,
    RegisterAuditCountUseCase,
    RegisterLossCommand,
    RegisterLossUseCase,
)
from candy_store.inventory.domain.errors import BatchNotFoundError, InsufficientStockError
from candy_store.inventory.domain.events import StockAdjusted, StockBatchReceived, StockDeducted
from candy_store.inventory.domain.value_objects import AdjustmentReason, BatchId, StockLocation
from candy_store.shared.domain.value_objects import ProductId
from tests.fakes import FakeInventoryUnitOfWork, FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

LOCATION = StockLocation.WAREHOUSE


@pytest.fixture
def uow() -> FakeInventoryUnitOfWork:
    return FakeInventoryUnitOfWork()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


async def receive(
    uow: FakeInventoryUnitOfWork,
    clock: FixedClock,
    product_id: ProductId,
    *,
    quantity: int,
    expires_in_days: int,
) -> BatchId:
    return await ReceiveStockBatchUseCase(uow, clock).execute(
        ReceiveStockBatchCommand(
            product_id=product_id,
            location=LOCATION,
            lot_number="LOTE-1",
            expiration=TODAY + timedelta(days=expires_in_days),
            quantity=quantity,
        )
    )


async def test_receive_creates_inventory_item_and_publishes_event(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    batch_id = await receive(uow, clock, product_id, quantity=12, expires_in_days=30)

    item = await uow.items.get(product_id, LOCATION)
    assert item is not None
    assert item.quantity_on_hand() == 12
    assert uow.commits == 1
    [event] = uow.published_events
    assert isinstance(event, StockBatchReceived)
    assert event.batch_id == batch_id


async def test_deduct_applies_fefo_across_received_batches(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    later = await receive(uow, clock, product_id, quantity=10, expires_in_days=60)
    sooner = await receive(uow, clock, product_id, quantity=3, expires_in_days=7)

    allocations = await DeductStockFefoUseCase(uow, clock).execute(
        DeductStockFefoCommand(product_id=product_id, location=LOCATION, quantity=5)
    )

    assert [(a.batch_id, a.quantity) for a in allocations] == [(sooner, 3), (later, 2)]
    assert isinstance(uow.published_events[-1], StockDeducted)


async def test_deduct_unknown_product_fails_without_commit(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    with pytest.raises(InsufficientStockError):
        await DeductStockFefoUseCase(uow, clock).execute(
            DeductStockFefoCommand(product_id=product_id, location=LOCATION, quantity=1)
        )
    assert uow.commits == 0


async def test_register_loss_and_audit(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    batch_id = await receive(uow, clock, product_id, quantity=10, expires_in_days=30)

    await RegisterLossUseCase(uow).execute(
        RegisterLossCommand(
            product_id=product_id,
            location=LOCATION,
            batch_id=batch_id,
            quantity=2,
            reason=AdjustmentReason.BREAKAGE,
        )
    )
    delta = await RegisterAuditCountUseCase(uow).execute(
        RegisterAuditCountCommand(
            product_id=product_id, location=LOCATION, batch_id=batch_id, counted_quantity=9
        )
    )

    assert delta == 1
    adjustments = [e for e in uow.published_events if isinstance(e, StockAdjusted)]
    assert [(e.delta, e.reason) for e in adjustments] == [
        (-2, AdjustmentReason.BREAKAGE),
        (1, AdjustmentReason.AUDIT),
    ]


async def test_loss_on_unknown_product_fails(uow: FakeInventoryUnitOfWork) -> None:
    with pytest.raises(BatchNotFoundError):
        await RegisterLossUseCase(uow).execute(
            RegisterLossCommand(
                product_id=ProductId.new(),
                location=LOCATION,
                batch_id=BatchId.new(),
                quantity=1,
                reason=AdjustmentReason.SHRINKAGE,
            )
        )
