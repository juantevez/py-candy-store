from datetime import UTC, date, datetime, timedelta

import pytest

from candy_store.purchasing.domain.errors import (
    InvalidPurchaseOrderError,
    InvalidPurchaseOrderStateError,
    InvalidRemittanceError,
    InvalidSupplierError,
    RemittanceMismatchError,
)
from candy_store.purchasing.domain.events import PurchaseOrderStatusChanged, RemittanceReceived
from candy_store.purchasing.domain.model import PurchaseOrder, RemittanceReceipt
from candy_store.purchasing.domain.value_objects import (
    Cuit,
    DeliveryLocation,
    PurchaseOrderId,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    ReceiptLine,
    RemittanceNumber,
    RemittanceReceiptId,
    SupplierId,
)
from candy_store.shared.domain.value_objects import Money, ProductId

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
TODAY = date(2026, 10, 14)
ALFAJOR = ProductId.new()
TURRON = ProductId.new()

# --- CUIT y número de remito ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["20-12345678-6", "20123456786", "30-71234567-1", " 30 71234567 1 "]
)
def test_valid_cuit(raw: str) -> None:
    assert len(Cuit(raw).value) == 11


@pytest.mark.parametrize("raw", ["20-12345678-5", "2012345678", "abcdefghijk", ""])
def test_invalid_cuit(raw: str) -> None:
    with pytest.raises(InvalidSupplierError):
        Cuit(raw)


def test_cuit_formatting() -> None:
    assert Cuit("20123456786").formatted == "20-12345678-6"


def test_remittance_number_is_normalized() -> None:
    assert RemittanceNumber("0001-00012345") == RemittanceNumber("00001-00012345")
    assert RemittanceNumber("0001-00012345").value == "00001-00012345"


@pytest.mark.parametrize("raw", ["12345", "001-00012345", "0001-123", "R-0001-00012345"])
def test_invalid_remittance_number(raw: str) -> None:
    with pytest.raises(InvalidRemittanceError):
        RemittanceNumber(raw)


# --- Orden de compra y recepción ------------------------------------------------------


def make_order(**quantities: int) -> PurchaseOrder:
    products = {"alfajor": ALFAJOR, "turron": TURRON}
    order = PurchaseOrder.issue(
        purchase_order_id=PurchaseOrderId.new(),
        supplier_id=SupplierId.new(),
        location=DeliveryLocation.WAREHOUSE,
        lines=[
            PurchaseOrderLine(products[name], qty, Money.of("900"))
            for name, qty in quantities.items()
        ],
        created_at=NOW,
    )
    order.pull_events()
    return order


def status_of(order: PurchaseOrder) -> PurchaseOrderStatus:
    # Evita que mypy "fije" el estado entre aserciones sucesivas sobre el mismo objeto.
    return order.status


def receive(
    order: PurchaseOrder, *lines: tuple[ProductId, int, int], number: str = "0001-00000001"
) -> RemittanceReceipt:
    """lines: (producto, cantidad, días hasta el vencimiento)"""
    return RemittanceReceipt.receive(
        receipt_id=RemittanceReceiptId.new(),
        order=order,
        remittance_number=RemittanceNumber(number),
        lines=[
            ReceiptLine(pid, qty, f"L-{days}", TODAY + timedelta(days=days))
            for pid, qty, days in lines
        ],
        received_at=NOW,
        today=TODAY,
    )


def test_issue_order() -> None:
    order = make_order(alfajor=100, turron=20)

    assert order.status is PurchaseOrderStatus.OPEN
    assert order.total_cost == Money.of("108000")


def test_order_needs_lines() -> None:
    with pytest.raises(InvalidPurchaseOrderError):
        make_order()


def test_partial_then_complete_delivery() -> None:
    order = make_order(alfajor=100, turron=20)

    receive(order, (ALFAJOR, 60, 90), (ALFAJOR, 20, 120), (TURRON, 20, 200))
    assert status_of(order) is PurchaseOrderStatus.PARTIALLY_RECEIVED
    assert {line.product_id: line.pending for line in order.lines} == {ALFAJOR: 20, TURRON: 0}

    receive(order, (ALFAJOR, 20, 90), number="0001-00000002")
    assert status_of(order) is PurchaseOrderStatus.RECEIVED
    events = [e for e in order.pull_events() if isinstance(e, PurchaseOrderStatusChanged)]
    assert [e.status for e in events] == [
        PurchaseOrderStatus.PARTIALLY_RECEIVED,
        PurchaseOrderStatus.RECEIVED,
    ]


def test_remittance_emits_integration_event_with_lots() -> None:
    order = make_order(alfajor=100)

    receipt = receive(order, (ALFAJOR, 60, 90), (ALFAJOR, 40, 120))

    [event] = receipt.pull_events()
    assert isinstance(event, RemittanceReceived)
    assert event.location is DeliveryLocation.WAREHOUSE
    assert event.received_on == TODAY
    assert [(line.lot_number, line.quantity) for line in event.lines] == [
        ("L-90", 60),
        ("L-120", 40),
    ]


def test_mismatch_reports_every_discrepancy_and_changes_nothing() -> None:
    order = make_order(alfajor=100)
    unexpected = ProductId.new()

    with pytest.raises(RemittanceMismatchError) as exc:
        receive(order, (ALFAJOR, 90, 90), (ALFAJOR, 20, 120), (unexpected, 5, 90))

    assert {(d.product_id, d.pending, d.delivered) for d in exc.value.discrepancies} == {
        (ALFAJOR, 100, 110),
        (unexpected, 0, 5),
    }
    assert order.status is PurchaseOrderStatus.OPEN
    assert order.lines[0].quantity_received == 0


def test_expired_goods_are_rejected() -> None:
    order = make_order(alfajor=100)

    with pytest.raises(InvalidRemittanceError, match="vencidos"):
        receive(order, (ALFAJOR, 10, -1))
    assert order.status is PurchaseOrderStatus.OPEN


def test_goods_expiring_today_are_accepted() -> None:
    order = make_order(alfajor=10)
    receive(order, (ALFAJOR, 10, 0))
    assert order.status is PurchaseOrderStatus.RECEIVED


def test_cancel_only_before_receiving() -> None:
    order = make_order(alfajor=10)
    order.cancel()
    assert order.status is PurchaseOrderStatus.CANCELLED

    partial = make_order(alfajor=10)
    receive(partial, (ALFAJOR, 5, 30))
    with pytest.raises(InvalidPurchaseOrderStateError):
        partial.cancel()


def test_close_partially_received_order() -> None:
    order = make_order(alfajor=10)
    with pytest.raises(InvalidPurchaseOrderStateError):
        order.close()

    receive(order, (ALFAJOR, 5, 30))
    order.close()

    assert order.status is PurchaseOrderStatus.CLOSED


@pytest.mark.parametrize("finish", ["cancel", "close"])
def test_finished_orders_do_not_accept_deliveries(finish: str) -> None:
    order = make_order(alfajor=10)
    if finish == "close":
        receive(order, (ALFAJOR, 5, 30))
        order.close()
    else:
        order.cancel()

    with pytest.raises(InvalidPurchaseOrderStateError):
        receive(order, (ALFAJOR, 1, 30), number="0001-00000099")


@pytest.mark.parametrize(("qty", "cost"), [(0, "10"), (5, "0")])
def test_invalid_order_lines(qty: int, cost: str) -> None:
    with pytest.raises(InvalidPurchaseOrderError):
        PurchaseOrderLine(ALFAJOR, qty, Money.of(cost))


def test_invalid_receipt_lines() -> None:
    with pytest.raises(InvalidRemittanceError):
        ReceiptLine(ALFAJOR, 0, "L1", TODAY)
    with pytest.raises(InvalidRemittanceError):
        ReceiptLine(ALFAJOR, 1, "   ", TODAY)
