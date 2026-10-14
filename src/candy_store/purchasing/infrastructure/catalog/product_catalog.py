"""Anti-corruption layer: catálogo de compras sobre la API pública del catálogo."""

from collections.abc import Iterable

from candy_store.catalog import public as catalog
from candy_store.purchasing.application.ports.product_catalog import (
    CatalogProduct,
    PurchasingCatalog,
)
from candy_store.shared.domain.value_objects import ProductId


class InProcessPurchasingCatalog(PurchasingCatalog):
    def __init__(self, facade: catalog.CatalogFacade) -> None:
        self._catalog = facade

    async def find(self, product_ids: Iterable[ProductId]) -> dict[ProductId, CatalogProduct]:
        products = await self._catalog.get_products(product_ids)
        return {
            pid: CatalogProduct(product_id=pid, name=info.name, is_kit=info.is_kit)
            for pid, info in products.items()
        }
