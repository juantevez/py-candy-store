from dataclasses import dataclass
from datetime import date, datetime

from candy_store.purchasing.domain.value_objects import (
    Cuit,
    DeliveryLocation,
    PurchaseOrderId,
    PurchaseOrderStatus,
    ReceiptLine,
    RemittanceNumber,
    RemittanceReceiptId,
    SupplierId,
)
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import Money


@dataclass(frozen=True, kw_only=True)
class SupplierRegistered(DomainEvent):
    supplier_id: SupplierId
    name: str
    cuit: Cuit


@dataclass(frozen=True, kw_only=True)
class PurchaseOrderIssued(DomainEvent):
    purchase_order_id: PurchaseOrderId
    supplier_id: SupplierId
    total_cost: Money


@dataclass(frozen=True, kw_only=True)
class PurchaseOrderStatusChanged(DomainEvent):
    purchase_order_id: PurchaseOrderId
    status: PurchaseOrderStatus


@dataclass(frozen=True, kw_only=True)
class RemittanceReceived(DomainEvent):
    """Evento de integración: inventory lo consume para ingresar los lotes al stock."""

    receipt_id: RemittanceReceiptId
    supplier_id: SupplierId
    remittance_number: RemittanceNumber
    purchase_order_id: PurchaseOrderId
    location: DeliveryLocation
    received_at: datetime
    # Fecha local del comercio en que llegó: contra ella se valida el vencimiento.
    received_on: date
    lines: tuple[ReceiptLine, ...]
