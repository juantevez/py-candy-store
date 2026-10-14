from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    product_id: ProductId
    name: str
    is_kit: bool


class PurchasingCatalog(ABC):
    @abstractmethod
    async def find(self, product_ids: Iterable[ProductId]) -> dict[ProductId, CatalogProduct]:
        """Los productos inexistentes no aparecen en el resultado."""
