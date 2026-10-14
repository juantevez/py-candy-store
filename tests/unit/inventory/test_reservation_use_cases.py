from datetime import timedelta

import pytest

from candy_store.inventory.application.use_cases.confirm_reservation import (
    ConfirmReservationUseCase,
)
from candy_store.inventory.application.use_cases.get_stock_summary import GetStockSummaryUseCase
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
    ReleaseReservationUseCase,
)
from candy_store.inventory.application.use_cases.reserve_stock import (
    ReserveStockCommand,
    ReserveStockUseCase,
)
from candy_store.inventory.domain.errors import InsufficientStockError, ReservationNotFoundError
from candy_store.inventory.domain.events import StockDeducted, StockReservationReleased
from candy_store.inventory.domain.value_objects import ReservationId, StockLocation
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


async def stock(uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId) -> None:
    await ReceiveStockBatchUseCase(uow, clock).execute(
        ReceiveStockBatchCommand(
            product_id=product_id,
            location=LOCATION,
            lot_number="L1",
            expiration=TODAY + timedelta(days=30),
            quantity=10,
        )
    )


async def reserve(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId, quantity: int
) -> ReservationId:
    receipt = await ReserveStockUseCase(uow, clock, TTL).execute(
        ReserveStockCommand(product_id=product_id, location=LOCATION, quantity=quantity)
    )
    assert receipt.expires_at == clock.current + TTL
    return receipt.reservation_id


async def test_reserve_then_confirm(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    await stock(uow, clock, product_id)
    reservation_id = await reserve(uow, clock, product_id, 4)

    allocations = await ConfirmReservationUseCase(uow, clock).execute(reservation_id)

    assert sum(a.quantity for a in allocations) == 4
    event = uow.published_events[-1]
    assert isinstance(event, StockDeducted)
    assert event.reservation_id == reservation_id


async def test_reserve_without_stock(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    with pytest.raises(InsufficientStockError):
        await reserve(uow, clock, product_id, 1)


async def test_release_reservation(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    await stock(uow, clock, product_id)
    reservation_id = await reserve(uow, clock, product_id, 10)

    await ReleaseReservationUseCase(uow).execute(reservation_id)

    summary = await GetStockSummaryUseCase(uow, clock).execute(product_id)
    assert summary.total_available == 10


async def test_confirm_unknown_reservation(uow: FakeInventoryUnitOfWork, clock: FixedClock) -> None:
    with pytest.raises(ReservationNotFoundError):
        await ConfirmReservationUseCase(uow, clock).execute(ReservationId.new())


async def test_release_unknown_reservation(uow: FakeInventoryUnitOfWork) -> None:
    with pytest.raises(ReservationNotFoundError):
        await ReleaseReservationUseCase(uow).execute(ReservationId.new())


async def test_release_expired_reservations_sweeps_only_past_ttl(
    uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    first, second = ProductId.new(), ProductId.new()
    await stock(uow, clock, first)
    await stock(uow, clock, second)
    await reserve(uow, clock, first, 3)
    await reserve(uow, clock, second, 2)
    clock.current = NOW + timedelta(minutes=5)
    fresh = await reserve(uow, clock, second, 1)

    clock.current = NOW + TTL + timedelta(seconds=1)
    released = await ReleaseExpiredReservationsUseCase(uow, clock).execute()

    assert released == 2
    summary = await GetStockSummaryUseCase(uow, clock).execute(second)
    [location] = summary.locations
    assert location.reserved == 1
    released_events = [e for e in uow.published_events if isinstance(e, StockReservationReleased)]
    assert all(e.expired for e in released_events)
    assert fresh not in {e.reservation_id for e in released_events}


async def test_stock_summary(
    uow: FakeInventoryUnitOfWork, clock: FixedClock, product_id: ProductId
) -> None:
    await stock(uow, clock, product_id)
    await reserve(uow, clock, product_id, 3)

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
