import pytest

from candy_store.inventory.domain.errors import InsufficientStockError, InvalidQuantityError
from candy_store.inventory.domain.services import allocate_fefo
from tests.unit.inventory.conftest import TODAY, make_batch


def test_takes_from_earliest_expiring_batch_first() -> None:
    later = make_batch(quantity=10, expires_in_days=60, lot="LATER")
    sooner = make_batch(quantity=10, expires_in_days=5, lot="SOONER")

    allocations = allocate_fefo([later, sooner], 4, TODAY)

    assert [(a.batch_id, a.quantity) for a in allocations] == [(sooner.batch_id, 4)]


def test_spans_multiple_batches_in_expiration_order() -> None:
    a = make_batch(quantity=3, expires_in_days=1)
    b = make_batch(quantity=5, expires_in_days=10)
    c = make_batch(quantity=100, expires_in_days=30)

    allocations = allocate_fefo([c, b, a], 10, TODAY)

    assert [(x.batch_id, x.quantity) for x in allocations] == [
        (a.batch_id, 3),
        (b.batch_id, 5),
        (c.batch_id, 2),
    ]


def test_same_expiration_falls_back_to_fifo() -> None:
    newer = make_batch(quantity=10, expires_in_days=20, received_minutes_ago=5)
    older = make_batch(quantity=10, expires_in_days=20, received_minutes_ago=500)

    allocations = allocate_fefo([newer, older], 1, TODAY)

    assert allocations[0].batch_id == older.batch_id


def test_skips_expired_and_empty_batches() -> None:
    expired = make_batch(quantity=50, expires_in_days=-1)
    empty = make_batch(quantity=0, expires_in_days=2)
    good = make_batch(quantity=5, expires_in_days=40)

    allocations = allocate_fefo([expired, empty, good], 5, TODAY)

    assert [a.batch_id for a in allocations] == [good.batch_id]


def test_batch_expiring_today_is_still_sellable() -> None:
    today_batch = make_batch(quantity=1, expires_in_days=0)

    assert allocate_fefo([today_batch], 1, TODAY)[0].batch_id == today_batch.batch_id


def test_insufficient_sellable_stock() -> None:
    batches = [
        make_batch(quantity=3, expires_in_days=5),
        make_batch(quantity=9, expires_in_days=-3),
    ]

    with pytest.raises(InsufficientStockError) as exc:
        allocate_fefo(batches, 4, TODAY)

    assert (exc.value.requested, exc.value.available) == (4, 3)


def test_does_not_mutate_batches() -> None:
    batch = make_batch(quantity=10, expires_in_days=5)

    allocate_fefo([batch], 7, TODAY)

    assert batch.quantity_on_hand == 10


@pytest.mark.parametrize("quantity", [0, -1])
def test_rejects_non_positive_quantity(quantity: int) -> None:
    with pytest.raises(InvalidQuantityError):
        allocate_fefo([make_batch(quantity=10, expires_in_days=5)], quantity, TODAY)
