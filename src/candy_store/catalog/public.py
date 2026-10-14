"""API pública del catálogo para otros contextos del monolito (ver inventory/public.py)."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.application.use_cases.get_products import GetProductsUseCase
from candy_store.shared.domain.value_objects import Money, ProductId

__all__ = ["CatalogFacade", "ProductInfo"]


@dataclass(frozen=True, slots=True)
class ProductInfo:
    product_id: ProductId
    name: str
    price: Money
    active: bool


class CatalogFacade:
    def __init__(self, uow_factory: Callable[[], CatalogUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def get_products(self, product_ids: Iterable[ProductId]) -> dict[ProductId, ProductInfo]:
        """Datos vigentes de los productos pedidos. Los inexistentes no aparecen."""
        views = await GetProductsUseCase(self._uow_factory()).execute(product_ids)
        return {
            pid: ProductInfo(product_id=pid, name=v.name, price=v.price, active=v.active)
            for pid, v in views.items()
        }
