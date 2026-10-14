"""Anti-corruption layer: ProductCatalog de sales sobre la API pública del catálogo."""

from collections.abc import Iterable

from candy_store.catalog import public as catalog
from candy_store.sales.application.ports.product_catalog import CatalogItem, ProductCatalog
from candy_store.shared.domain.value_objects import ProductId


class InProcessProductCatalog(ProductCatalog):
    def __init__(self, facade: catalog.CatalogFacade) -> None:
        self._catalog = facade

    async def find(self, product_ids: Iterable[ProductId]) -> dict[ProductId, CatalogItem]:
        products = await self._catalog.get_products(product_ids)
        return {
            pid: CatalogItem(
                product_id=pid, name=info.name, unit_price=info.price, sellable=info.active
            )
            for pid, info in products.items()
        }
