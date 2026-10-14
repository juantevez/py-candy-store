"""API pública del catálogo para otros contextos del monolito (ver inventory/public.py)."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.application.use_cases.get_products import GetProductsUseCase
from candy_store.shared.domain.value_objects import Money, ProductId

__all__ = ["CatalogFacade", "KitComponentInfo", "ProductInfo"]


@dataclass(frozen=True, slots=True)
class ProductInfo:
    product_id: ProductId
    name: str
    price: Money
    active: bool
    is_kit: bool


@dataclass(frozen=True, slots=True)
class KitComponentInfo:
    product_id: ProductId
    quantity: int


class CatalogFacade:
    def __init__(self, uow_factory: Callable[[], CatalogUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def get_products(self, product_ids: Iterable[ProductId]) -> dict[ProductId, ProductInfo]:
        """Datos vigentes de los productos pedidos. Los inexistentes no aparecen."""
        views = await GetProductsUseCase(self._uow_factory()).execute(product_ids)
        return {
            pid: ProductInfo(
                product_id=pid, name=v.name, price=v.price, active=v.active, is_kit=v.is_kit
            )
            for pid, v in views.items()
        }

    async def get_kit_recipe(self, product_id: ProductId) -> tuple[KitComponentInfo, ...] | None:
        """Receta vigente del kit, o None si el producto no existe o no es un kit."""
        views = await GetProductsUseCase(self._uow_factory()).execute([product_id])
        view = views.get(product_id)
        if view is None or not view.is_kit:
            return None
        return tuple(KitComponentInfo(c.product_id, c.quantity) for c in view.components)
