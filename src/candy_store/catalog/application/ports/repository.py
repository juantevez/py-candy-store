from abc import ABC, abstractmethod
from collections.abc import Iterable

from candy_store.catalog.domain.model import Product
from candy_store.catalog.domain.value_objects import Sku
from candy_store.shared.application.unit_of_work import UnitOfWork
from candy_store.shared.domain.value_objects import ProductId


class CatalogRepository(ABC):
    @abstractmethod
    async def get(self, product_id: ProductId) -> Product | None: ...

    @abstractmethod
    async def get_by_sku(self, sku: Sku) -> Product | None: ...

    @abstractmethod
    async def get_many(self, product_ids: Iterable[ProductId]) -> list[Product]: ...

    @abstractmethod
    async def list_all(self, *, limit: int, offset: int) -> list[Product]:
        """Productos ordenados por SKU."""

    @abstractmethod
    async def add(self, product: Product) -> None: ...


class CatalogUnitOfWork(UnitOfWork):
    products: CatalogRepository
