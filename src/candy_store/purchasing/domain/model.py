"""Agregados de compras: Supplier, PurchaseOrder y RemittanceReceipt (remito)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from candy_store.purchasing.domain.errors import (
    Discrepancy,
    InvalidPurchaseOrderError,
    InvalidPurchaseOrderStateError,
    InvalidRemittanceError,
    InvalidSupplierError,
    RemittanceMismatchError,
)
from candy_store.purchasing.domain.events import (
    PurchaseOrderIssued,
    PurchaseOrderStatusChanged,
    RemittanceReceived,
    SupplierRegistered,
)
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
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.domain.value_objects import Money, ProductId

_MAX_NAME_LENGTH = 120


class Supplier(AggregateRoot):
    def __init__(
        self,
        *,
        supplier_id: SupplierId,
        name: str,
        cuit: Cuit,
        active: bool = True,
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        normalized = " ".join(name.split())
        if not normalized or len(normalized) > _MAX_NAME_LENGTH:
            raise InvalidSupplierError("Nombre de proveedor inválido")
        self.supplier_id = supplier_id
        self.name = normalized
        self.cuit = cuit
        self.active = active

    @classmethod
    def register(cls, *, supplier_id: SupplierId, name: str, cuit: Cuit) -> Supplier:
        supplier = cls(supplier_id=supplier_id, name=name, cuit=cuit)
        supplier._record(SupplierRegistered(supplier_id=supplier_id, name=supplier.name, cuit=cuit))
        return supplier


class PurchaseOrder(AggregateRoot):
    """Orden de compra a un proveedor. Lleva la cuenta de lo recibido por producto."""

    def __init__(
        self,
        *,
        purchase_order_id: PurchaseOrderId,
        supplier_id: SupplierId,
        location: DeliveryLocation,
        lines: Sequence[PurchaseOrderLine],
        status: PurchaseOrderStatus,
        created_at: datetime,
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        if not lines:
            raise InvalidPurchaseOrderError("La orden de compra debe tener al menos una línea")
        product_ids = [line.product_id for line in lines]
        if len(set(product_ids)) != len(product_ids):
            raise InvalidPurchaseOrderError("Cada producto debe aparecer en una sola línea")
        if len({line.unit_cost.currency for line in lines}) != 1:
            raise InvalidPurchaseOrderError("Todas las líneas deben estar en la misma moneda")
        self.purchase_order_id = purchase_order_id
        self.supplier_id = supplier_id
        self.location = location
        self._lines = {line.product_id: line for line in lines}
        self.status = status
        self.created_at = created_at

    @classmethod
    def issue(
        cls,
        *,
        purchase_order_id: PurchaseOrderId,
        supplier_id: SupplierId,
        location: DeliveryLocation,
        lines: Sequence[PurchaseOrderLine],
        created_at: datetime,
    ) -> PurchaseOrder:
        if any(line.quantity_received for line in lines):
            raise InvalidPurchaseOrderError("Una orden nueva no puede tener mercadería recibida")
        order = cls(
            purchase_order_id=purchase_order_id,
            supplier_id=supplier_id,
            location=location,
            lines=lines,
            status=PurchaseOrderStatus.OPEN,
            created_at=created_at,
        )
        order._record(
            PurchaseOrderIssued(
                purchase_order_id=purchase_order_id,
                supplier_id=supplier_id,
                total_cost=order.total_cost,
            )
        )
        return order

    @property
    def lines(self) -> tuple[PurchaseOrderLine, ...]:
        return tuple(self._lines.values())

    @property
    def total_cost(self) -> Money:
        lines = self.lines
        total = Money.zero(lines[0].unit_cost.currency)
        for line in lines:
            total += line.unit_cost * line.quantity_ordered
        return total

    @property
    def accepts_deliveries(self) -> bool:
        return self.status in {PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTIALLY_RECEIVED}

    def check_delivery(self, delivered: dict[ProductId, int]) -> None:
        """Verifica que lo entregado no exceda lo pendiente. Informa todas las
        diferencias juntas."""
        if not self.accepts_deliveries:
            raise InvalidPurchaseOrderStateError(
                f"La orden {self.purchase_order_id} está {self.status} y no recibe mercadería"
            )
        discrepancies = tuple(
            Discrepancy(product_id=pid, pending=self._pending(pid), delivered=quantity)
            for pid, quantity in delivered.items()
            if quantity > self._pending(pid)
        )
        if discrepancies:
            raise RemittanceMismatchError(discrepancies)

    def register_delivery(self, delivered: dict[ProductId, int]) -> None:
        self.check_delivery(delivered)
        for pid, quantity in delivered.items():
            line = self._lines[pid]
            self._lines[pid] = PurchaseOrderLine(
                product_id=pid,
                quantity_ordered=line.quantity_ordered,
                unit_cost=line.unit_cost,
                quantity_received=line.quantity_received + quantity,
            )
        complete = all(line.pending == 0 for line in self._lines.values())
        self._change_status(
            PurchaseOrderStatus.RECEIVED if complete else PurchaseOrderStatus.PARTIALLY_RECEIVED
        )

    def cancel(self) -> None:
        if self.status is not PurchaseOrderStatus.OPEN:
            raise InvalidPurchaseOrderStateError(
                "Solo se puede cancelar una orden sin mercadería recibida; "
                "si ya llegó una parte, cerrala"
            )
        self._change_status(PurchaseOrderStatus.CANCELLED)

    def close(self) -> None:
        """Da por terminada una orden con faltantes que ya no van a llegar."""
        if self.status is not PurchaseOrderStatus.PARTIALLY_RECEIVED:
            raise InvalidPurchaseOrderStateError(
                "Solo se cierra una orden parcialmente recibida; si no llegó nada, cancelala"
            )
        self._change_status(PurchaseOrderStatus.CLOSED)

    def _pending(self, product_id: ProductId) -> int:
        line = self._lines.get(product_id)
        return line.pending if line else 0

    def _change_status(self, status: PurchaseOrderStatus) -> None:
        if status is self.status:
            return
        self.status = status
        self._record(
            PurchaseOrderStatusChanged(purchase_order_id=self.purchase_order_id, status=status)
        )


class RemittanceReceipt(AggregateRoot):
    """Recepción de un remito del proveedor en el depósito. Es un registro inmutable."""

    def __init__(
        self,
        *,
        receipt_id: RemittanceReceiptId,
        supplier_id: SupplierId,
        remittance_number: RemittanceNumber,
        purchase_order_id: PurchaseOrderId,
        location: DeliveryLocation,
        received_at: datetime,
        lines: Sequence[ReceiptLine],
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        if not lines:
            raise InvalidRemittanceError("El remito debe tener al menos una línea")
        self.receipt_id = receipt_id
        self.supplier_id = supplier_id
        self.remittance_number = remittance_number
        self.purchase_order_id = purchase_order_id
        self.location = location
        self.received_at = received_at
        self.lines = tuple(lines)

    @classmethod
    def receive(
        cls,
        *,
        receipt_id: RemittanceReceiptId,
        order: PurchaseOrder,
        remittance_number: RemittanceNumber,
        lines: Sequence[ReceiptLine],
        received_at: datetime,
        today: date,
    ) -> RemittanceReceipt:
        """Registra el remito contra su orden de compra (y actualiza la orden).

        No se acepta mercadería vencida: el vencimiento se compara contra la fecha
        local de recepción.
        """
        expired = sorted({line.lot_number for line in lines if line.expiration < today})
        if expired:
            raise InvalidRemittanceError(f"Lotes vencidos: {', '.join(expired)}")
        receipt = cls(
            receipt_id=receipt_id,
            supplier_id=order.supplier_id,
            remittance_number=remittance_number,
            purchase_order_id=order.purchase_order_id,
            location=order.location,
            received_at=received_at,
            lines=lines,
        )
        order.register_delivery(receipt.quantities_by_product())
        receipt._record(
            RemittanceReceived(
                receipt_id=receipt_id,
                supplier_id=order.supplier_id,
                remittance_number=remittance_number,
                purchase_order_id=order.purchase_order_id,
                location=order.location,
                received_at=received_at,
                received_on=today,
                lines=receipt.lines,
            )
        )
        return receipt

    def quantities_by_product(self) -> dict[ProductId, int]:
        totals: dict[ProductId, int] = {}
        for line in self.lines:
            totals[line.product_id] = totals.get(line.product_id, 0) + line.quantity
        return totals
