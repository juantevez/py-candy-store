from datetime import timedelta

import pytest

from candy_store.inventory.application.dto import StockLine
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
from candy_store.inventory.domain.errors import (
    BatchNotFoundError,
    InvalidQuantityError,
    StockShortageError,
)
from candy_store.inventory.domain.events import StockAdjusted, StockBatchReceived, StockDeducted
from candy_store.inventory.domain.value_objects import (
    AdjustmentReason,
    BatchId,
    StockLocation,
    StockReference,
)
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


def sale(reference: str, *lines: tuple[ProductId, int]) -> DeductStockFefoCommand:
    return DeductStockFefoCommand(
        reference=StockReference(reference),
        location=LOCATION,
        lines=[StockLine(product_id=pid, quantity=qty) for pid, qty in lines],
    )


async def test_deduct_applies_fefo_across_received_batches(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    later = await receive(uow, clock, product_id, quantity=10, expires_in_days=60)
    sooner = await receive(uow, clock, product_id, quantity=3, expires_in_days=7)

    result = await DeductStockFefoUseCase(uow, clock).execute(sale("T-1", (product_id, 5)))

    [line] = result.lines
    assert [(a.batch_id, a.quantity) for a in line.allocations] == [(sooner, 3), (later, 2)]
    event = uow.published_events[-1]
    assert isinstance(event, StockDeducted)
    assert event.reference == StockReference("T-1")


async def test_multi_line_sale_deducts_every_product(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    alfajor, chicle = ProductId.new(), ProductId.new()
    await receive(uow, clock, alfajor, quantity=10, expires_in_days=30)
    await receive(uow, clock, chicle, quantity=10, expires_in_days=30)

    result = await DeductStockFefoUseCase(uow, clock).execute(
        sale("T-1", (alfajor, 2), (chicle, 3), (alfajor, 1))  # alfajor repetido: se suma
    )

    assert {line.product_id: line.quantity for line in result.lines} == {alfajor: 3, chicle: 3}
    for pid, remaining in [(alfajor, 7), (chicle, 7)]:
        item = await uow.items.get(pid, LOCATION)
        assert item is not None
        assert item.quantity_on_hand() == remaining


async def test_sale_is_all_or_nothing_and_reports_every_shortage(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    alfajor, chicle, turron = ProductId.new(), ProductId.new(), ProductId.new()
    await receive(uow, clock, alfajor, quantity=10, expires_in_days=30)
    await receive(uow, clock, chicle, quantity=1, expires_in_days=30)

    with pytest.raises(StockShortageError) as exc:
        await DeductStockFefoUseCase(uow, clock).execute(
            sale("T-1", (alfajor, 2), (chicle, 3), (turron, 1))
        )

    assert {(s.product_id, s.requested, s.available) for s in exc.value.shortages} == {
        (chicle, 3, 1),
        (turron, 1, 0),
    }
    item = await uow.items.get(alfajor, LOCATION)
    assert item is not None
    assert item.quantity_on_hand() == 10


async def test_repeating_a_sale_reference_does_not_deduct_twice(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    await receive(uow, clock, product_id, quantity=10, expires_in_days=30)
    use_case = DeductStockFefoUseCase(uow, clock)

    first = await use_case.execute(sale("T-1", (product_id, 4)))
    retry = await use_case.execute(sale("T-1", (product_id, 4)))

    assert retry == first
    item = await uow.items.get(product_id, LOCATION)
    assert item is not None
    assert item.quantity_on_hand() == 6


@pytest.mark.parametrize("lines", [[], [(0, 0)]])
async def test_sale_needs_positive_lines(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, lines: list[tuple[int, int]]
) -> None:
    command = sale("T-1", *[(ProductId.new(), qty) for _, qty in lines])
    with pytest.raises(InvalidQuantityError):
        await DeductStockFefoUseCase(uow, clock).execute(command)


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
