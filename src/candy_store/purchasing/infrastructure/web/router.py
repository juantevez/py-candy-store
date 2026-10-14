"""Adaptador HTTP de compras: proveedores, órdenes de compra y recepción de remitos."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

from candy_store.purchasing.application.ports.product_catalog import PurchasingCatalog
from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.application.use_cases.create_purchase_order import (
    CreatePurchaseOrderCommand,
    CreatePurchaseOrderUseCase,
    OrderLineInput,
)
from candy_store.purchasing.application.use_cases.manage_purchase_order import (
    CancelPurchaseOrderUseCase,
    ClosePurchaseOrderUseCase,
)
from candy_store.purchasing.application.use_cases.process_remittance_receipt import (
    ProcessRemittanceCommand,
    ProcessRemittanceReceiptUseCase,
)
from candy_store.purchasing.application.use_cases.queries import (
    GetPurchaseOrderUseCase,
    GetSupplierUseCase,
    ListSuppliersUseCase,
    PurchaseOrderView,
    SupplierView,
)
from candy_store.purchasing.application.use_cases.register_supplier import (
    RegisterSupplierCommand,
    RegisterSupplierUseCase,
)
from candy_store.purchasing.domain.errors import (
    DuplicateRemittanceError,
    DuplicateSupplierError,
    InvalidPurchaseOrderStateError,
    ProductNotPurchasableError,
    PurchaseOrderNotFoundError,
    RemittanceMismatchError,
    SupplierNotFoundError,
)
from candy_store.purchasing.domain.value_objects import (
    DeliveryLocation,
    PurchaseOrderId,
    ReceiptLine,
    SupplierId,
)
from candy_store.purchasing.infrastructure.persistence.unit_of_work import (
    SqlAlchemyPurchasingUnitOfWork,
)
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.web.dependencies import ClockDep, SessionFactoryDep
from candy_store.shared.infrastructure.web.errors import ErrorStatusMap

ERROR_STATUS: ErrorStatusMap = {
    SupplierNotFoundError: status.HTTP_404_NOT_FOUND,
    PurchaseOrderNotFoundError: status.HTTP_404_NOT_FOUND,
    DuplicateSupplierError: status.HTTP_409_CONFLICT,
    DuplicateRemittanceError: status.HTTP_409_CONFLICT,
    InvalidPurchaseOrderStateError: status.HTTP_409_CONFLICT,
    RemittanceMismatchError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ProductNotPurchasableError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _purchasing_uow(session_factory: SessionFactoryDep) -> PurchasingUnitOfWork:
    return SqlAlchemyPurchasingUnitOfWork(session_factory)


def _catalog(request: Request) -> PurchasingCatalog:
    catalog: PurchasingCatalog = request.app.state.purchasing_catalog
    return catalog


UowDep = Annotated[PurchasingUnitOfWork, Depends(_purchasing_uow)]
CatalogDep = Annotated[PurchasingCatalog, Depends(_catalog)]

# --- Schemas -------------------------------------------------------------------------


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplierIn(_Request):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    cuit: Annotated[str, StringConstraints(strip_whitespace=True, max_length=13)]


class SupplierOut(BaseModel):
    supplier_id: UUID
    name: str
    cuit: str
    active: bool

    @classmethod
    def of(cls, view: SupplierView) -> SupplierOut:
        return cls(
            supplier_id=view.supplier_id.value, name=view.name, cuit=view.cuit, active=view.active
        )


class OrderLineIn(_Request):
    product_id: UUID
    quantity: PositiveInt
    unit_cost: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class PurchaseOrderIn(_Request):
    supplier_id: UUID
    location: DeliveryLocation = DeliveryLocation.WAREHOUSE
    lines: Annotated[list[OrderLineIn], Field(min_length=1, max_length=200)]


class ReceiptLineIn(_Request):
    product_id: UUID
    quantity: PositiveInt
    lot_number: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    expiration: date


class RemittanceIn(_Request):
    purchase_order_id: UUID
    remittance_number: Annotated[str, StringConstraints(strip_whitespace=True, max_length=14)]
    lines: Annotated[list[ReceiptLineIn], Field(min_length=1, max_length=500)]


class OrderLineOut(BaseModel):
    product_id: UUID
    quantity_ordered: int
    quantity_received: int
    pending: int
    unit_cost: Decimal


class ReceiptLineOut(BaseModel):
    product_id: UUID
    quantity: int
    lot_number: str
    expiration: date


class ReceiptOut(BaseModel):
    receipt_id: UUID
    remittance_number: str
    received_at: datetime
    lines: list[ReceiptLineOut]


class PurchaseOrderOut(BaseModel):
    purchase_order_id: UUID
    supplier_id: UUID
    location: str
    status: str
    created_at: datetime
    total_cost: Decimal
    currency: str
    lines: list[OrderLineOut]
    receipts: list[ReceiptOut]

    @classmethod
    def of(cls, view: PurchaseOrderView) -> PurchaseOrderOut:
        return cls(
            purchase_order_id=view.purchase_order_id.value,
            supplier_id=view.supplier_id.value,
            location=view.location,
            status=view.status,
            created_at=view.created_at,
            total_cost=view.total_cost.amount,
            currency=view.total_cost.currency,
            lines=[
                OrderLineOut(
                    product_id=line.product_id.value,
                    quantity_ordered=line.quantity_ordered,
                    quantity_received=line.quantity_received,
                    pending=line.pending,
                    unit_cost=line.unit_cost.amount,
                )
                for line in view.lines
            ],
            receipts=[
                ReceiptOut(
                    receipt_id=r.receipt_id.value,
                    remittance_number=r.remittance_number,
                    received_at=r.received_at,
                    lines=[
                        ReceiptLineOut(
                            product_id=line.product_id.value,
                            quantity=line.quantity,
                            lot_number=line.lot_number,
                            expiration=line.expiration,
                        )
                        for line in r.lines
                    ],
                )
                for r in view.receipts
            ],
        )


# --- Endpoints --------------------------------------------------------------------------

router = APIRouter(prefix="/purchasing", tags=["purchasing"])


@router.post("/suppliers", status_code=status.HTTP_201_CREATED)
async def register_supplier(body: SupplierIn, uow: UowDep) -> SupplierOut:
    supplier_id = await RegisterSupplierUseCase(uow).execute(
        RegisterSupplierCommand(name=body.name, cuit=body.cuit)
    )
    return SupplierOut.of(await GetSupplierUseCase(uow).execute(supplier_id))


@router.get("/suppliers")
async def list_suppliers(uow: UowDep) -> list[SupplierOut]:
    return [SupplierOut.of(view) for view in await ListSuppliersUseCase(uow).execute()]


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def create_purchase_order(
    body: PurchaseOrderIn, uow: UowDep, catalog: CatalogDep, clock: ClockDep
) -> PurchaseOrderOut:
    order_id = await CreatePurchaseOrderUseCase(uow, catalog, clock).execute(
        CreatePurchaseOrderCommand(
            supplier_id=SupplierId(body.supplier_id),
            location=body.location,
            lines=[
                OrderLineInput(ProductId(line.product_id), line.quantity, line.unit_cost)
                for line in body.lines
            ],
        )
    )
    return PurchaseOrderOut.of(await GetPurchaseOrderUseCase(uow).execute(order_id))


@router.get("/orders/{purchase_order_id}")
async def get_purchase_order(purchase_order_id: UUID, uow: UowDep) -> PurchaseOrderOut:
    view = await GetPurchaseOrderUseCase(uow).execute(PurchaseOrderId(purchase_order_id))
    return PurchaseOrderOut.of(view)


@router.post("/orders/{purchase_order_id}/cancellation")
async def cancel_purchase_order(purchase_order_id: UUID, uow: UowDep) -> PurchaseOrderOut:
    order_id = PurchaseOrderId(purchase_order_id)
    await CancelPurchaseOrderUseCase(uow).execute(order_id)
    return PurchaseOrderOut.of(await GetPurchaseOrderUseCase(uow).execute(order_id))


@router.post("/orders/{purchase_order_id}/closure")
async def close_purchase_order(purchase_order_id: UUID, uow: UowDep) -> PurchaseOrderOut:
    order_id = PurchaseOrderId(purchase_order_id)
    await ClosePurchaseOrderUseCase(uow).execute(order_id)
    return PurchaseOrderOut.of(await GetPurchaseOrderUseCase(uow).execute(order_id))


@router.post("/remittances", status_code=status.HTTP_201_CREATED)
async def receive_remittance(body: RemittanceIn, uow: UowDep, clock: ClockDep) -> PurchaseOrderOut:
    """Recepción en depósito: cruza el remito con la orden de compra. Si coincide,
    la mercadería ingresa al stock (de forma asincrónica, vía evento)."""
    order_id = PurchaseOrderId(body.purchase_order_id)
    await ProcessRemittanceReceiptUseCase(uow, clock).execute(
        ProcessRemittanceCommand(
            purchase_order_id=order_id,
            remittance_number=body.remittance_number,
            lines=[
                ReceiptLine(
                    product_id=ProductId(line.product_id),
                    quantity=line.quantity,
                    lot_number=line.lot_number,
                    expiration=line.expiration,
                )
                for line in body.lines
            ],
        )
    )
    return PurchaseOrderOut.of(await GetPurchaseOrderUseCase(uow).execute(order_id))
