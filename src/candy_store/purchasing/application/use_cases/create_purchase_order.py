from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from candy_store.purchasing.application.ports.product_catalog import PurchasingCatalog
from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.domain.errors import ProductNotPurchasableError, SupplierNotFoundError
from candy_store.purchasing.domain.model import PurchaseOrder
from candy_store.purchasing.domain.value_objects import (
    DeliveryLocation,
    PurchaseOrderId,
    PurchaseOrderLine,
    SupplierId,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import Money, ProductId


@dataclass(frozen=True, slots=True)
class OrderLineInput:
    product_id: ProductId
    quantity: int
    unit_cost: Decimal


@dataclass(frozen=True, slots=True)
class CreatePurchaseOrderCommand:
    supplier_id: SupplierId
    lines: Sequence[OrderLineInput]
    location: DeliveryLocation = DeliveryLocation.WAREHOUSE


class CreatePurchaseOrderUseCase:
    def __init__(self, uow: PurchasingUnitOfWork, catalog: PurchasingCatalog, clock: Clock) -> None:
        self._uow = uow
        self._catalog = catalog
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: CreatePurchaseOrderCommand) -> PurchaseOrderId:
        lines = [
            PurchaseOrderLine(
                product_id=line.product_id,
                quantity_ordered=line.quantity,
                unit_cost=Money(line.unit_cost),
            )
            for line in command.lines
        ]
        products = await self._catalog.find(line.product_id for line in lines)
        not_purchasable = [
            str(line.product_id)
            for line in lines
            if line.product_id not in products or products[line.product_id].is_kit
        ]
        if not_purchasable:
            raise ProductNotPurchasableError(not_purchasable)

        async with self._uow as uow:
            supplier = await uow.suppliers.get(command.supplier_id)
            if supplier is None or not supplier.active:
                raise SupplierNotFoundError(f"No existe el proveedor {command.supplier_id}")
            order = PurchaseOrder.issue(
                purchase_order_id=PurchaseOrderId.new(),
                supplier_id=command.supplier_id,
                location=command.location,
                lines=lines,
                created_at=self._clock.now(),
            )
            await uow.orders.add(order)
            await uow.commit()
        return order.purchase_order_id
