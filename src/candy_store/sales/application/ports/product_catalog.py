from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from candy_store.shared.domain.value_objects import Money, ProductId


@dataclass(frozen=True, slots=True)
class CatalogItem:
    product_id: ProductId
    name: str
    unit_price: Money
    sellable: bool


class ProductCatalog(ABC):
    """Precios y nombres vigentes. El precio lo decide el catálogo, nunca el cliente."""

    @abstractmethod
    async def find(self, product_ids: Iterable[ProductId]) -> dict[ProductId, CatalogItem]: ...
