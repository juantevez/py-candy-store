from datetime import timedelta

import pytest

from candy_store.inventory.domain.errors import (
    BatchNotFoundError,
    DuplicateBatchError,
    DuplicateReservationError,
    ExpiredBatchReceivedError,
    InsufficientStockError,
    ReservationNotFoundError,
)
from candy_store.inventory.domain.events import (
    StockAdjusted,
    StockBatchReceived,
    StockDeducted,
    StockReservationReleased,
    StockReserved,
)
from candy_store.inventory.domain.value_objects import (
    AdjustmentReason,
    BatchId,
    ExpirationDate,
    LotNumber,
    ReservationId,
)
from tests.unit.inventory.conftest import NOW, TODAY, make_batch, make_item

# --- Ingreso de lotes ---------------------------------------------------------


def test_receive_batch_adds_stock_and_records_event() -> None:
    item = make_item()
    batch_id = BatchId.new()

    item.receive_batch(
        batch_id=batch_id,
        lot_number=LotNumber(" ab-123 "),
        expiration=ExpirationDate(TODAY + timedelta(days=90)),
        quantity=24,
        received_at=NOW,
        today=TODAY,
    )

    assert item.quantity_on_hand() == 24
    [event] = item.pull_events()
    assert isinstance(event, StockBatchReceived)
    assert event.lot_number == LotNumber("AB-123")
    assert event.quantity == 24


def test_cannot_receive_already_expired_batch() -> None:
    with pytest.raises(ExpiredBatchReceivedError):
        make_item().receive_batch(
            batch_id=BatchId.new(),
            lot_number=LotNumber("X"),
            expiration=ExpirationDate(TODAY - timedelta(days=1)),
            quantity=1,
            received_at=NOW,
            today=TODAY,
        )


def test_cannot_receive_same_batch_twice() -> None:
    batch = make_batch(quantity=1, expires_in_days=10)
    item = make_item(batch)

    with pytest.raises(DuplicateBatchError):
        item.receive_batch(
            batch_id=batch.batch_id,
            lot_number=batch.lot_number,
            expiration=batch.expiration,
            quantity=1,
            received_at=NOW,
            today=TODAY,
        )


# --- Descuento FEFO -----------------------------------------------------------


def test_deduct_consumes_earliest_expiring_batches() -> None:
    sooner = make_batch(quantity=5, expires_in_days=3)
    later = make_batch(quantity=10, expires_in_days=30)
    item = make_item(later, sooner)

    item.deduct(7, TODAY)

    assert sooner.quantity_on_hand == 0
    assert later.quantity_on_hand == 8
    [event] = item.pull_events()
    assert isinstance(event, StockDeducted)
    assert event.reservation_id is None
    assert [(a.batch_id, a.quantity) for a in event.allocations] == [
        (sooner.batch_id, 5),
        (later.batch_id, 2),
    ]


def test_deduct_never_sells_expired_stock() -> None:
    item = make_item(make_batch(quantity=100, expires_in_days=-1))

    with pytest.raises(InsufficientStockError):
        item.deduct(1, TODAY)


def test_failed_deduct_leaves_state_untouched() -> None:
    batch = make_batch(quantity=3, expires_in_days=10)
    item = make_item(batch)

    with pytest.raises(InsufficientStockError):
        item.deduct(4, TODAY)

    assert batch.quantity_on_hand == 3
    assert item.pull_events() == []


def test_pos_sale_cannot_take_stock_reserved_online() -> None:
    item = make_item(make_batch(quantity=10, expires_in_days=10))
    item.reserve(
        reservation_id=ReservationId.new(),
        quantity=8,
        expires_at=NOW + timedelta(minutes=15),
        today=TODAY,
    )

    with pytest.raises(InsufficientStockError) as exc:
        item.deduct(3, TODAY)

    assert exc.value.available == 2


# --- Reservas online ----------------------------------------------------------


def test_reserve_reduces_available_but_not_on_hand() -> None:
    item = make_item(make_batch(quantity=10, expires_in_days=10))

    item.reserve(
        reservation_id=ReservationId.new(),
        quantity=4,
        expires_at=NOW + timedelta(minutes=15),
        today=TODAY,
    )

    assert item.quantity_on_hand() == 10
    assert item.available_quantity(TODAY) == 6
    assert isinstance(item.pull_events()[0], StockReserved)


def test_cannot_overbook_reservations() -> None:
    item = make_item(make_batch(quantity=5, expires_in_days=10))
    expires_at = NOW + timedelta(minutes=15)
    item.reserve(reservation_id=ReservationId.new(), quantity=3, expires_at=expires_at, today=TODAY)

    with pytest.raises(InsufficientStockError):
        item.reserve(
            reservation_id=ReservationId.new(), quantity=3, expires_at=expires_at, today=TODAY
        )


def test_duplicate_reservation_id_is_rejected() -> None:
    item = make_item(make_batch(quantity=5, expires_in_days=10))
    rid = ReservationId.new()
    item.reserve(reservation_id=rid, quantity=1, expires_at=NOW, today=TODAY)

    with pytest.raises(DuplicateReservationError):
        item.reserve(reservation_id=rid, quantity=1, expires_at=NOW, today=TODAY)


def test_confirm_reservation_deducts_by_fefo() -> None:
    sooner = make_batch(quantity=2, expires_in_days=3)
    later = make_batch(quantity=10, expires_in_days=30)
    item = make_item(later, sooner)
    rid = ReservationId.new()
    item.reserve(reservation_id=rid, quantity=5, expires_at=NOW, today=TODAY)
    item.pull_events()

    item.confirm_reservation(rid, TODAY)

    assert item.reserved_quantity() == 0
    assert (sooner.quantity_on_hand, later.quantity_on_hand) == (0, 7)
    [event] = item.pull_events()
    assert isinstance(event, StockDeducted)
    assert event.reservation_id == rid


def test_confirm_fails_if_stock_was_lost_meanwhile_and_keeps_reservation() -> None:
    batch = make_batch(quantity=5, expires_in_days=10)
    item = make_item(batch)
    rid = ReservationId.new()
    item.reserve(reservation_id=rid, quantity=5, expires_at=NOW, today=TODAY)
    item.register_loss(batch_id=batch.batch_id, quantity=2, reason=AdjustmentReason.BREAKAGE)

    with pytest.raises(InsufficientStockError):
        item.confirm_reservation(rid, TODAY)

    assert item.reserved_quantity() == 5
    assert batch.quantity_on_hand == 3


def test_after_a_loss_first_reservation_to_confirm_wins() -> None:
    batch = make_batch(quantity=10, expires_in_days=10)
    item = make_item(batch)
    first, second = ReservationId.new(), ReservationId.new()
    item.reserve(reservation_id=first, quantity=6, expires_at=NOW, today=TODAY)
    item.reserve(reservation_id=second, quantity=4, expires_at=NOW, today=TODAY)
    item.register_loss(batch_id=batch.batch_id, quantity=1, reason=AdjustmentReason.SHRINKAGE)

    item.confirm_reservation(first, TODAY)

    with pytest.raises(InsufficientStockError):
        item.confirm_reservation(second, TODAY)
    assert batch.quantity_on_hand == 3
    assert item.reserved_quantity() == 4


def test_release_reservation_frees_stock() -> None:
    item = make_item(make_batch(quantity=5, expires_in_days=10))
    rid = ReservationId.new()
    item.reserve(reservation_id=rid, quantity=5, expires_at=NOW, today=TODAY)
    item.pull_events()

    item.release_reservation(rid)

    assert item.available_quantity(TODAY) == 5
    [event] = item.pull_events()
    assert isinstance(event, StockReservationReleased)
    assert event.expired is False


def test_release_expired_reservations_only_releases_past_ttl() -> None:
    item = make_item(make_batch(quantity=10, expires_in_days=10))
    stale, fresh = ReservationId.new(), ReservationId.new()
    item.reserve(
        reservation_id=stale, quantity=3, expires_at=NOW - timedelta(minutes=1), today=TODAY
    )
    item.reserve(
        reservation_id=fresh, quantity=2, expires_at=NOW + timedelta(minutes=10), today=TODAY
    )

    released = item.release_expired_reservations(NOW)

    assert released == (stale,)
    assert [r.reservation_id for r in item.reservations] == [fresh]


def test_unknown_reservation() -> None:
    with pytest.raises(ReservationNotFoundError):
        make_item().confirm_reservation(ReservationId.new(), TODAY)


# --- Ajustes ------------------------------------------------------------------


def test_register_loss_on_specific_batch() -> None:
    batch = make_batch(quantity=10, expires_in_days=10)
    item = make_item(batch)

    item.register_loss(batch_id=batch.batch_id, quantity=3, reason=AdjustmentReason.BREAKAGE)

    assert batch.quantity_on_hand == 7
    [event] = item.pull_events()
    assert isinstance(event, StockAdjusted)
    assert (event.delta, event.reason) == (-3, AdjustmentReason.BREAKAGE)


def test_loss_greater_than_batch_stock_fails() -> None:
    batch = make_batch(quantity=2, expires_in_days=10)

    with pytest.raises(InsufficientStockError):
        make_item(batch).register_loss(
            batch_id=batch.batch_id, quantity=3, reason=AdjustmentReason.SHRINKAGE
        )


def test_audit_reason_must_use_audit_count() -> None:
    batch = make_batch(quantity=2, expires_in_days=10)

    with pytest.raises(ValueError, match="auditoría"):
        make_item(batch).register_loss(
            batch_id=batch.batch_id, quantity=1, reason=AdjustmentReason.AUDIT
        )


@pytest.mark.parametrize(("counted", "expected_delta"), [(12, 2), (7, -3)])
def test_audit_count_sets_quantity_and_reports_delta(counted: int, expected_delta: int) -> None:
    batch = make_batch(quantity=10, expires_in_days=10)
    item = make_item(batch)

    delta = item.register_audit_count(batch_id=batch.batch_id, counted_quantity=counted)

    assert delta == expected_delta
    assert batch.quantity_on_hand == counted
    [event] = item.pull_events()
    assert isinstance(event, StockAdjusted)
    assert event.reason is AdjustmentReason.AUDIT


def test_audit_count_without_difference_records_nothing() -> None:
    batch = make_batch(quantity=10, expires_in_days=10)
    item = make_item(batch)

    assert item.register_audit_count(batch_id=batch.batch_id, counted_quantity=10) == 0
    assert item.pull_events() == []


def test_unknown_batch() -> None:
    with pytest.raises(BatchNotFoundError):
        make_item().register_audit_count(batch_id=BatchId.new(), counted_quantity=1)


def test_write_off_expired_batches() -> None:
    expired = make_batch(quantity=4, expires_in_days=-2)
    good = make_batch(quantity=6, expires_in_days=5)
    item = make_item(expired, good)

    assert item.write_off_expired(TODAY) == 4
    assert expired.quantity_on_hand == 0
    assert good.quantity_on_hand == 6
    [event] = item.pull_events()
    assert isinstance(event, StockAdjusted)
    assert (event.batch_id, event.reason) == (expired.batch_id, AdjustmentReason.EXPIRED)
