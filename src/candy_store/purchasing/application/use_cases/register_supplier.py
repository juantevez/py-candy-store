from dataclasses import dataclass

from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.domain.errors import DuplicateSupplierError
from candy_store.purchasing.domain.model import Supplier
from candy_store.purchasing.domain.value_objects import Cuit, SupplierId
from candy_store.shared.application.retry import retry_on_concurrency


@dataclass(frozen=True, slots=True)
class RegisterSupplierCommand:
    name: str
    cuit: str


class RegisterSupplierUseCase:
    def __init__(self, uow: PurchasingUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, command: RegisterSupplierCommand) -> SupplierId:
        cuit = Cuit(command.cuit)
        async with self._uow as uow:
            if await uow.suppliers.get_by_cuit(cuit) is not None:
                raise DuplicateSupplierError(f"Ya existe un proveedor con CUIT {cuit}")
            supplier = Supplier.register(supplier_id=SupplierId.new(), name=command.name, cuit=cuit)
            await uow.suppliers.add(supplier)
            await uow.commit()
        return supplier.supplier_id
