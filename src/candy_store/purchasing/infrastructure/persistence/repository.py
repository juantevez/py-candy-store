from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.purchasing.application.ports.repository import (
    PurchaseOrderRepository,
    RemittanceRepository,
    SupplierRepository,
)
from candy_store.purchasing.domain.model import PurchaseOrder, RemittanceReceipt, Supplier
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
from candy_store.purchasing.infrastructure.persistence.models import (
    PurchaseOrderLineRow,
    PurchaseOrderRow,
    RemittanceReceiptLineRow,
    RemittanceReceiptRow,
    SupplierRow,
)
from candy_store.shared.domain.value_objects import Money, ProductId


class SqlAlchemySupplierRepository(SupplierRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._seen: dict[SupplierId, tuple[Supplier, SupplierRow | None]] = {}

    async def get(self, supplier_id: SupplierId) -> Supplier | None:
        if supplier_id in self._seen:
            return self._seen[supplier_id][0]
        row = await self._session.get(SupplierRow, supplier_id.value)
        return self._track(row) if row else None

    async def get_by_cuit(self, cuit: Cuit) -> Supplier | None:
        row = await self._session.scalar(select(SupplierRow).where(SupplierRow.cuit == cuit.value))
        return self._track(row) if row else None

    async def list_all(self) -> list[Supplier]:
        rows = await self._session.scalars(select(SupplierRow).order_by(SupplierRow.name))
        return [self._track(row) for row in rows]

    async def add(self, supplier: Supplier) -> None:
        self._seen[supplier.supplier_id] = (supplier, None)

    @property
    def seen(self) -> list[Supplier]:
        return [supplier for supplier, _ in self._seen.values()]

    def persist_seen(self) -> None:
        for supplier_id, (supplier, row) in self._seen.items():
            if row is None:
                row = SupplierRow(id=supplier_id.value, version=0)
                self._session.add(row)
                self._seen[supplier_id] = (supplier, row)
            row.name = supplier.name
            row.cuit = supplier.cuit.value
            row.active = supplier.active
            row.version = supplier.version + 1
            supplier.version = row.version

    def _track(self, row: SupplierRow) -> Supplier:
        supplier_id = SupplierId(row.id)
        if supplier_id not in self._seen:
            supplier = Supplier(
                supplier_id=supplier_id,
                name=row.name,
                cuit=Cuit(row.cuit),
                active=row.active,
                version=row.version,
            )
            self._seen[supplier_id] = (supplier, row)
        return self._seen[supplier_id][0]


class SqlAlchemyPurchaseOrderRepository(PurchaseOrderRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._seen: dict[PurchaseOrderId, tuple[PurchaseOrder, PurchaseOrderRow | None]] = {}

    async def get(self, purchase_order_id: PurchaseOrderId) -> PurchaseOrder | None:
        if purchase_order_id in self._seen:
            return self._seen[purchase_order_id][0]
        row = await self._session.get(PurchaseOrderRow, purchase_order_id.value)
        if row is None:
            return None
        order = PurchaseOrder(
            purchase_order_id=purchase_order_id,
            supplier_id=SupplierId(row.supplier_id),
            location=DeliveryLocation(row.location),
            lines=[
                PurchaseOrderLine(
                    product_id=ProductId(line.product_id),
                    quantity_ordered=line.quantity_ordered,
                    unit_cost=Money(line.unit_cost, row.currency),
                    quantity_received=line.quantity_received,
                )
                for line in row.lines
            ],
            status=PurchaseOrderStatus(row.status),
            created_at=row.created_at,
            version=row.version,
        )
        self._seen[purchase_order_id] = (order, row)
        return order

    async def add(self, order: PurchaseOrder) -> None:
        self._seen[order.purchase_order_id] = (order, None)

    @property
    def seen(self) -> list[PurchaseOrder]:
        return [order for order, _ in self._seen.values()]

    def persist_seen(self) -> None:
        for order_id, (order, row) in self._seen.items():
            if row is None:
                row = PurchaseOrderRow(
                    id=order_id.value,
                    supplier_id=order.supplier_id.value,
                    location=order.location.value,
                    created_at=order.created_at,
                    currency=order.total_cost.currency,
                    version=0,
                    lines=[
                        PurchaseOrderLineRow(
                            product_id=line.product_id.value,
                            quantity_ordered=line.quantity_ordered,
                            unit_cost=line.unit_cost.amount,
                            quantity_received=line.quantity_received,
                        )
                        for line in order.lines
                    ],
                )
                self._session.add(row)
                self._seen[order_id] = (order, row)
            row.status = order.status.value
            received = {line.product_id.value: line.quantity_received for line in order.lines}
            for line_row in row.lines:
                line_row.quantity_received = received[line_row.product_id]
            row.version = order.version + 1
            order.version = row.version


class SqlAlchemyRemittanceRepository(RemittanceRepository):
    """Los remitos son inmutables: solo se insertan."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._new: list[RemittanceReceipt] = []
        self._loaded: list[RemittanceReceipt] = []

    async def get(self, receipt_id: RemittanceReceiptId) -> RemittanceReceipt | None:
        row = await self._session.get(RemittanceReceiptRow, receipt_id.value)
        return self._load(row) if row else None

    async def find_by_number(
        self, supplier_id: SupplierId, remittance_number: RemittanceNumber
    ) -> RemittanceReceipt | None:
        row = await self._session.scalar(
            select(RemittanceReceiptRow).where(
                RemittanceReceiptRow.supplier_id == supplier_id.value,
                RemittanceReceiptRow.remittance_number == remittance_number.value,
            )
        )
        return self._load(row) if row else None

    async def list_for_order(self, purchase_order_id: PurchaseOrderId) -> list[RemittanceReceipt]:
        rows = await self._session.scalars(
            select(RemittanceReceiptRow)
            .where(RemittanceReceiptRow.purchase_order_id == purchase_order_id.value)
            .order_by(RemittanceReceiptRow.received_at)
        )
        return [self._load(row) for row in rows]

    async def add(self, receipt: RemittanceReceipt) -> None:
        self._new.append(receipt)

    @property
    def seen(self) -> list[RemittanceReceipt]:
        return [*self._loaded, *self._new]

    def persist_seen(self) -> None:
        for receipt in self._new:
            self._session.add(
                RemittanceReceiptRow(
                    id=receipt.receipt_id.value,
                    supplier_id=receipt.supplier_id.value,
                    remittance_number=receipt.remittance_number.value,
                    purchase_order_id=receipt.purchase_order_id.value,
                    location=receipt.location.value,
                    received_at=receipt.received_at,
                    version=1,
                    lines=[
                        RemittanceReceiptLineRow(
                            line_number=number,
                            product_id=line.product_id.value,
                            quantity=line.quantity,
                            lot_number=line.lot_number,
                            expiration=line.expiration,
                        )
                        for number, line in enumerate(receipt.lines, start=1)
                    ],
                )
            )
        self._new.clear()

    def _load(self, row: RemittanceReceiptRow) -> RemittanceReceipt:
        receipt = RemittanceReceipt(
            receipt_id=RemittanceReceiptId(row.id),
            supplier_id=SupplierId(row.supplier_id),
            remittance_number=RemittanceNumber(row.remittance_number),
            purchase_order_id=PurchaseOrderId(row.purchase_order_id),
            location=DeliveryLocation(row.location),
            received_at=row.received_at,
            lines=[
                ReceiptLine(
                    product_id=ProductId(line.product_id),
                    quantity=line.quantity,
                    lot_number=line.lot_number,
                    expiration=line.expiration,
                )
                for line in row.lines
            ],
            version=row.version,
        )
        self._loaded.append(receipt)
        return receipt
