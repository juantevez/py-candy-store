"""Entidad Product del catálogo maestro."""

from __future__ import annotations

from candy_store.catalog.domain.errors import InvalidProductError
from candy_store.catalog.domain.events import (
    ProductAvailabilityChanged,
    ProductCreated,
    ProductPriceChanged,
)
from candy_store.catalog.domain.value_objects import Sku
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.domain.value_objects import Money, ProductId

_MAX_NAME_LENGTH = 120


def _validate_name(name: str) -> str:
    normalized = " ".join(name.split())
    if not normalized:
        raise InvalidProductError("El nombre del producto no puede estar vacío")
    if len(normalized) > _MAX_NAME_LENGTH:
        raise InvalidProductError(f"El nombre excede {_MAX_NAME_LENGTH} caracteres")
    return normalized


def _validate_price(price: Money) -> Money:
    if price.amount <= 0:
        raise InvalidProductError(f"El precio debe ser positivo: {price}")
    return price


class Product(AggregateRoot):
    """Producto vendible. Un producto inactivo sigue existiendo (historial de ventas)
    pero no se puede vender."""

    def __init__(
        self,
        *,
        product_id: ProductId,
        sku: Sku,
        name: str,
        price: Money,
        active: bool = True,
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        self.product_id = product_id
        self.sku = sku
        self.name = _validate_name(name)
        self.price = _validate_price(price)
        self.active = active

    @classmethod
    def create(cls, *, product_id: ProductId, sku: Sku, name: str, price: Money) -> Product:
        product = cls(product_id=product_id, sku=sku, name=name, price=price)
        product._record(
            ProductCreated(product_id=product_id, sku=sku, name=product.name, price=product.price)
        )
        return product

    def rename(self, name: str) -> None:
        self.name = _validate_name(name)

    def change_price(self, price: Money) -> None:
        new_price = _validate_price(price)
        if new_price == self.price:
            return
        old_price, self.price = self.price, new_price
        self._record(
            ProductPriceChanged(
                product_id=self.product_id, old_price=old_price, new_price=new_price
            )
        )

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        self._record(ProductAvailabilityChanged(product_id=self.product_id, active=active))
