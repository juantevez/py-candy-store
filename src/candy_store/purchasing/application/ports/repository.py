"""PurchasingRepository: persistencia de proveedores, órdenes de compra y remitos."""

from abc import ABC, abstractmethod

from candy_store.purchasing.domain.model import PurchaseOrder, RemittanceReceipt, Supplier
from candy_store.purchasing.domain.value_objects import (
    Cuit,
    PurchaseOrderId,
    RemittanceNumber,
    RemittanceReceiptId,
    SupplierId,
)
from candy_store.shared.application.unit_of_work import UnitOfWork


class SupplierRepository(ABC):
    @abstractmethod
    async def get(self, supplier_id: SupplierId) -> Supplier | None: ...

    @abstractmethod
    async def get_by_cuit(self, cuit: Cuit) -> Supplier | None: ...

    @abstractmethod
    async def list_all(self) -> list[Supplier]: ...

    @abstractmethod
    async def add(self, supplier: Supplier) -> None: ...


class PurchaseOrderRepository(ABC):
    @abstractmethod
    async def get(self, purchase_order_id: PurchaseOrderId) -> PurchaseOrder | None: ...

    @abstractmethod
    async def add(self, order: PurchaseOrder) -> None: ...


class RemittanceRepository(ABC):
    @abstractmethod
    async def get(self, receipt_id: RemittanceReceiptId) -> RemittanceReceipt | None: ...

    @abstractmethod
    async def find_by_number(
        self, supplier_id: SupplierId, remittance_number: RemittanceNumber
    ) -> RemittanceReceipt | None: ...

    @abstractmethod
    async def list_for_order(self, purchase_order_id: PurchaseOrderId) -> list[RemittanceReceipt]:
        """Remitos de la orden, en orden de recepción."""

    @abstractmethod
    async def add(self, receipt: RemittanceReceipt) -> None: ...


class PurchasingUnitOfWork(UnitOfWork):
    suppliers: SupplierRepository
    orders: PurchaseOrderRepository
    remittances: RemittanceRepository
