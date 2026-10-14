from datetime import timedelta

import pytest

from candy_store.inventory.application.dto import StockLine
from candy_store.inventory.application.use_cases.confirm_reservation import (
    ConfirmReservationUseCase,
)
from candy_store.inventory.application.use_cases.get_stock_summary import GetStockSummaryUseCase
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.application.use_cases.register_loss_adjustment import (
    RegisterLossCommand,
    RegisterLossUseCase,
)
from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
    ReleaseReservationUseCase,
)
from candy_store.inventory.application.use_cases.reserve_stock import (
    ReserveStockCommand,
    ReserveStockUseCase,
)
from candy_store.inventory.domain.errors import ReservationNotFoundError, StockShortageError
from candy_store.inventory.domain.events import StockDeducted, StockReservationReleased
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
TTL = timedelta(minutes=15)


@pytest.fixture
def uow() -> FakeInventoryUnitOfWork:
    return FakeInventoryUnitOfWork()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


async def stock(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId, quantity: int = 10
) -> BatchId:
    return await ReceiveStockBatchUseCase(uow, clock).execute(
        ReceiveStockBatchCommand(
            product_id=product_id,
            location=LOCATION,
            lot_number="L1",
            expiration=TODAY + timedelta(days=30),
            quantity=quantity,
        )
    )


async def reserve(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, reference: str, *lines: tuple[ProductId, int]
) -> StockReference:
    ref = StockReference(reference)
    receipt = await ReserveStockUseCase(uow, clock, TTL).execute(
        ReserveStockCommand(
            reference=ref,
            location=LOCATION,
            lines=[StockLine(product_id=pid, quantity=qty) for pid, qty in lines],
        )
    )
    assert receipt.expires_at == clock.current + TTL
    return ref


async def available(uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId) -> int:
    return (await GetStockSummaryUseCase(uow, clock).execute(product_id)).total_available


async def test_order_reservation_then_confirmation(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    alfajor, chicle = ProductId.new(), ProductId.new()
    await stock(uow, clock, alfajor)
    await stock(uow, clock, chicle)
    ref = await reserve(uow, clock, "ORD-1", (alfajor, 4), (chicle, 2))
    assert await available(uow, clock, alfajor) == 6

    result = await ConfirmReservationUseCase(uow, clock).execute(ref)

    assert {line.product_id: line.quantity for line in result.lines} == {alfajor: 4, chicle: 2}
    deducted = [e for e in uow.published_events if isinstance(e, StockDeducted)]
    assert {e.product_id for e in deducted} == {alfajor, chicle}
    assert all(e.reference == ref and e.from_reservation for e in deducted)
    assert await available(uow, clock, alfajor) == 6


async def test_reservation_is_all_or_nothing(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    alfajor, chicle = ProductId.new(), ProductId.new()
    await stock(uow, clock, alfajor)
    await stock(uow, clock, chicle, quantity=1)

    with pytest.raises(StockShortageError):
        await reserve(uow, clock, "ORD-1", (alfajor, 4), (chicle, 2))

    assert await available(uow, clock, alfajor) == 10


async def test_reservation_and_confirmation_are_idempotent(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    await stock(uow, clock, product_id)
    ref = await reserve(uow, clock, "ORD-1", (product_id, 4))
    await reserve(uow, clock, "ORD-1", (product_id, 4))  # reintento del checkout
    assert await available(uow, clock, product_id) == 6

    confirm = ConfirmReservationUseCase(uow, clock)
    first = await confirm.execute(ref)
    duplicate_webhook = await confirm.execute(ref)

    assert duplicate_webhook == first
    item = await uow.items.get(product_id, LOCATION)
    assert item is not None
    assert item.quantity_on_hand() == 6


async def test_failed_confirmation_rolls_back_every_line(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    alfajor, chicle = ProductId.new(), ProductId.new()
    await stock(uow, clock, alfajor)
    chicle_batch = await stock(uow, clock, chicle, quantity=2)
    ref = await reserve(uow, clock, "ORD-1", (alfajor, 4), (chicle, 2))
    await RegisterLossUseCase(uow).execute(
        RegisterLossCommand(
            product_id=chicle,
            location=LOCATION,
            batch_id=chicle_batch,
            quantity=1,
            reason=AdjustmentReason.BREAKAGE,
        )
    )

    with pytest.raises(StockShortageError) as exc:
        await ConfirmReservationUseCase(uow, clock).execute(ref)

    [shortage] = exc.value.shortages
    assert (shortage.product_id, shortage.requested, shortage.available) == (chicle, 2, 1)
    alfajor_item = await uow.items.get(alfajor, LOCATION)
    assert alfajor_item is not None
    assert alfajor_item.quantity_on_hand() == 10
    assert alfajor_item.has_reservation(ref)


async def test_release_order_reservation(uow: FakeInventoryUnitOfWork, clock: FixedClock) -> None:
    alfajor, chicle = ProductId.new(), ProductId.new()
    await stock(uow, clock, alfajor)
    await stock(uow, clock, chicle)
    ref = await reserve(uow, clock, "ORD-1", (alfajor, 10), (chicle, 10))

    await ReleaseReservationUseCase(uow).execute(ref)
    await ReleaseReservationUseCase(uow).execute(ref)  # idempotente

    assert await available(uow, clock, alfajor) == 10
    assert await available(uow, clock, chicle) == 10


async def test_confirm_unknown_reservation(uow: FakeInventoryUnitOfWork, clock: FixedClock) -> None:
    with pytest.raises(ReservationNotFoundError):
        await ConfirmReservationUseCase(uow, clock).execute(StockReference("NO-EXISTE"))


async def test_release_unknown_reservation(uow: FakeInventoryUnitOfWork) -> None:
    with pytest.raises(ReservationNotFoundError):
        await ReleaseReservationUseCase(uow).execute(StockReference("NO-EXISTE"))


async def test_release_expired_reservations_sweeps_only_past_ttl(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    first, second = ProductId.new(), ProductId.new()
    await stock(uow, clock, first)
    await stock(uow, clock, second)
    await reserve(uow, clock, "ORD-1", (first, 3), (second, 2))
    clock.current = NOW + timedelta(minutes=5)
    fresh = await reserve(uow, clock, "ORD-2", (second, 1))

    clock.current = NOW + TTL + timedelta(seconds=1)
    released = await ReleaseExpiredReservationsUseCase(uow, clock).execute()

    assert released == 2
    summary = await GetStockSummaryUseCase(uow, clock).execute(second)
    [location] = summary.locations
    assert location.reserved == 1
    released_events = [e for e in uow.published_events if isinstance(e, StockReservationReleased)]
    assert {e.reference for e in released_events} == {StockReference("ORD-1")}
    assert all(e.expired for e in released_events)
    assert fresh not in {e.reference for e in released_events}


async def test_stock_summary(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    await stock(uow, clock, product_id)
    await reserve(uow, clock, "ORD-1", (product_id, 3))

    summary = await GetStockSummaryUseCase(uow, clock).execute(product_id)

    [location] = summary.locations
    assert (location.on_hand, location.sellable, location.reserved, location.available) == (
        10,
        10,
        3,
        7,
    )
    assert [b.lot_number for b in location.batches] == ["L1"]


async def test_stock_summary_of_unknown_product(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    summary = await GetStockSummaryUseCase(uow, clock).execute(ProductId.new())
    assert summary.locations == ()
    assert summary.total_available == 0
