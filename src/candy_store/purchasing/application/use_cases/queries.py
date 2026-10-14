"""Consultas de compras (solo lectura)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.domain.errors import (
    PurchaseOrderNotFoundError,
    SupplierNotFoundError,
)
from candy_store.purchasing.domain.model import PurchaseOrder, RemittanceReceipt, Supplier
from candy_store.purchasing.domain.value_objects import (
    DeliveryLocation,
    PurchaseOrderId,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    ReceiptLine,
    RemittanceReceiptId,
    SupplierId,
)
from candy_store.shared.domain.value_objects import Money


@dataclass(frozen=True, slots=True)
class SupplierView:
    supplier_id: SupplierId
    name: str
    cuit: str
    active: bool

    @classmethod
    def of(cls, supplier: Supplier) -> SupplierView:
        return cls(supplier.supplier_id, supplier.name, supplier.cuit.formatted, supplier.active)


@dataclass(frozen=True, slots=True)
class ReceiptView:
    receipt_id: RemittanceReceiptId
    remittance_number: str
    received_at: datetime
    lines: tuple[ReceiptLine, ...]

    @classmethod
    def of(cls, receipt: RemittanceReceipt) -> ReceiptView:
        return cls(
            receipt.receipt_id, receipt.remittance_number.value, receipt.received_at, receipt.lines
        )


@dataclass(frozen=True, slots=True)
class PurchaseOrderView:
    purchase_order_id: PurchaseOrderId
    supplier_id: SupplierId
    location: DeliveryLocation
    status: PurchaseOrderStatus
    created_at: datetime
    lines: tuple[PurchaseOrderLine, ...]
    total_cost: Money
    receipts: tuple[ReceiptView, ...]

    @classmethod
    def of(cls, order: PurchaseOrder, receipts: list[RemittanceReceipt]) -> PurchaseOrderView:
        return cls(
            purchase_order_id=order.purchase_order_id,
            supplier_id=order.supplier_id,
            location=order.location,
            status=order.status,
            created_at=order.created_at,
            lines=order.lines,
            total_cost=order.total_cost,
            receipts=tuple(ReceiptView.of(r) for r in receipts),
        )


class GetPurchaseOrderUseCase:
    def __init__(self, uow: PurchasingUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, purchase_order_id: PurchaseOrderId) -> PurchaseOrderView:
        async with self._uow as uow:
            order = await uow.orders.get(purchase_order_id)
            if order is None:
                raise PurchaseOrderNotFoundError(f"No existe la orden {purchase_order_id}")
            receipts = await uow.remittances.list_for_order(purchase_order_id)
        return PurchaseOrderView.of(order, receipts)


class GetSupplierUseCase:
    def __init__(self, uow: PurchasingUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, supplier_id: SupplierId) -> SupplierView:
        async with self._uow as uow:
            supplier = await uow.suppliers.get(supplier_id)
        if supplier is None:
            raise SupplierNotFoundError(f"No existe el proveedor {supplier_id}")
        return SupplierView.of(supplier)


class ListSuppliersUseCase:
    def __init__(self, uow: PurchasingUnitOfWork) -> None:
        self._uow = uow

    async def execute(self) -> list[SupplierView]:
        async with self._uow as uow:
            suppliers = await uow.suppliers.list_all()
        return [SupplierView.of(s) for s in suppliers]
