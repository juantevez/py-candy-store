from datetime import timedelta

import pytest

from candy_store.inventory.application.use_cases.receive_supplier_delivery import (
    DeliveryLine,
    ReceiveSupplierDeliveryCommand,
    ReceiveSupplierDeliveryUseCase,
)
from candy_store.inventory.domain.events import StockBatchReceived
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.domain.value_objects import ProductId
from tests.fakes import FakeInventoryUnitOfWork
from tests.unit.inventory.conftest import NOW, TODAY

ALFAJOR = ProductId.new()
TURRON = ProductId.new()


def delivery(
    *lines: tuple[ProductId, str, int, int], received_days_ago: int = 0
) -> ReceiveSupplierDeliveryCommand:
    """lines: (producto, lote, días hasta el vencimiento desde hoy, cantidad)"""
    return ReceiveSupplierDeliveryCommand(
        reference=StockReference("REMITO-1"),
        location=StockLocation.WAREHOUSE,
        received_at=NOW - timedelta(days=received_days_ago),
        received_on=TODAY - timedelta(days=received_days_ago),
        lines=[
            DeliveryLine(pid, lot, TODAY + timedelta(days=days), qty)
            for pid, lot, days, qty in lines
        ],
    )


async def test_creates_one_batch_per_line_even_for_the_same_product() -> None:
    uow = FakeInventoryUnitOfWork()

    created = await ReceiveSupplierDeliveryUseCase(uow).execute(
        delivery((ALFAJOR, "A-1", 90, 60), (ALFAJOR, "A-2", 120, 40), (TURRON, "T-1", 200, 20))
    )

    assert created == 3
    alfajor = await uow.items.get(ALFAJOR, StockLocation.WAREHOUSE)
    assert alfajor is not None
    assert sorted(b.lot_number.value for b in alfajor.batches) == ["A-1", "A-2"]
    assert alfajor.quantity_on_hand() == 100
    received = [e for e in uow.published_events if isinstance(e, StockBatchReceived)]
    assert len(received) == 3


async def test_redelivered_event_does_not_duplicate_stock() -> None:
    uow = FakeInventoryUnitOfWork()
    use_case = ReceiveSupplierDeliveryUseCase(uow)
    command = delivery((ALFAJOR, "A-1", 90, 60))

    await use_case.execute(command)
    await use_case.execute(command)

    alfajor = await uow.items.get(ALFAJOR, StockLocation.WAREHOUSE)
    assert alfajor is not None
    assert alfajor.quantity_on_hand() == 60


@pytest.mark.parametrize("received_days_ago", [1, 3])
async def test_expiration_is_checked_against_reception_date_not_processing_date(
    received_days_ago: int,
) -> None:
    """Un lote que vencía el día que llegó se aceptó en compras; si el evento se
    procesa más tarde no se debe rechazar."""
    uow = FakeInventoryUnitOfWork()

    await ReceiveSupplierDeliveryUseCase(uow).execute(
        delivery((ALFAJOR, "A-1", -received_days_ago, 5), received_days_ago=received_days_ago)
    )

    alfajor = await uow.items.get(ALFAJOR, StockLocation.WAREHOUSE)
    assert alfajor is not None
    assert alfajor.quantity_on_hand() == 5
