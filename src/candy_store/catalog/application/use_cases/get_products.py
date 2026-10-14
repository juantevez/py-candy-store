"""Consultas de catálogo (solo lectura)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.domain.errors import ProductNotFoundError
from candy_store.catalog.domain.model import Product
from candy_store.shared.domain.value_objects import Money, ProductId


@dataclass(frozen=True, slots=True)
class ProductView:
    product_id: ProductId
    sku: str
    name: str
    price: Money
    active: bool

    @classmethod
    def of(cls, product: Product) -> ProductView:
        return cls(
            product_id=product.product_id,
            sku=product.sku.value,
            name=product.name,
            price=product.price,
            active=product.active,
        )


class GetProductUseCase:
    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, product_id: ProductId) -> ProductView:
        async with self._uow as uow:
            product = await uow.products.get(product_id)
        if product is None:
            raise ProductNotFoundError(f"No existe el producto {product_id}")
        return ProductView.of(product)


class GetProductsUseCase:
    """Varios productos por id. Los inexistentes simplemente no aparecen."""

    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, product_ids: Iterable[ProductId]) -> dict[ProductId, ProductView]:
        async with self._uow as uow:
            products = await uow.products.get_many(set(product_ids))
        return {p.product_id: ProductView.of(p) for p in products}


class ListProductsUseCase:
    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    async def execute(self, *, limit: int = 100, offset: int = 0) -> list[ProductView]:
        async with self._uow as uow:
            products = await uow.products.list_all(limit=limit, offset=offset)
        return [ProductView.of(p) for p in products]
