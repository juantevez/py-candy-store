from abc import ABC, abstractmethod

from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.shared.application.unit_of_work import UnitOfWork
from candy_store.shared.domain.value_objects import ProductId


class InventoryRepository(ABC):
    @abstractmethod
    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        """Carga el agregado completo. La implementación debe aplicar bloqueo optimista
        con `InventoryItem.version` al confirmar."""

    @abstractmethod
    async def add(self, item: InventoryItem) -> None: ...


class InventoryUnitOfWork(UnitOfWork):
    items: InventoryRepository
