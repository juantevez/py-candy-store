from datetime import UTC, date, datetime, timedelta

import pytest

from candy_store.inventory.domain.model import InventoryItem, StockBatch
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
)
from candy_store.shared.domain.value_objects import ProductId

TODAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)


def make_batch(
    *,
    quantity: int,
    expires_in_days: int,
    lot: str = "L1",
    received_minutes_ago: int = 0,
) -> StockBatch:
    return StockBatch(
        batch_id=BatchId.new(),
        lot_number=LotNumber(lot),
        expiration=ExpirationDate(TODAY + timedelta(days=expires_in_days)),
        quantity_on_hand=quantity,
        received_at=NOW - timedelta(minutes=received_minutes_ago),
    )


def make_item(*batches: StockBatch) -> InventoryItem:
    return InventoryItem(
        product_id=ProductId.new(), location=StockLocation.STORE_FRONT, batches=batches
    )


@pytest.fixture
def product_id() -> ProductId:
    return ProductId.new()
