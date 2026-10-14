"""Entidad Product del catálogo maestro."""

from __future__ import annotations

from collections.abc import Sequence

from candy_store.catalog.domain.errors import InvalidKitError, InvalidProductError
from candy_store.catalog.domain.events import (
    KitCompositionChanged,
    ProductAvailabilityChanged,
    ProductCreated,
    ProductPriceChanged,
)
from candy_store.catalog.domain.value_objects import KitComponent, Sku
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


def _validate_components(
    kit_id: ProductId, components: Sequence[KitComponent]
) -> tuple[KitComponent, ...]:
    if not components:
        raise InvalidKitError("Un kit debe tener al menos un componente")
    product_ids = [c.product_id for c in components]
    if len(set(product_ids)) != len(product_ids):
        raise InvalidKitError("Cada producto debe aparecer una sola vez en la receta")
    if kit_id in product_ids:
        raise InvalidKitError("Un kit no puede contenerse a sí mismo")
    if sum(c.quantity for c in components) < 2:
        raise InvalidKitError("Un kit debe reunir al menos dos unidades")
    return tuple(components)


class Product(AggregateRoot):
    """Producto vendible. Un producto inactivo sigue existiendo (historial de ventas)
    pero no se puede vender.

    Un kit es un producto con receta: se arma de antemano en inventario
    consumiendo sus componentes y se vende como cualquier otro producto. Que sea
    kit o no se decide al crearlo; la receta se puede cambiar y solo afecta a los
    kits que se armen después.
    """

    def __init__(
        self,
        *,
        product_id: ProductId,
        sku: Sku,
        name: str,
        price: Money,
        active: bool = True,
        components: Sequence[KitComponent] = (),
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        self.product_id = product_id
        self.sku = sku
        self.name = _validate_name(name)
        self.price = _validate_price(price)
        self.active = active
        self.components = _validate_components(product_id, components) if components else ()

    @property
    def is_kit(self) -> bool:
        return bool(self.components)

    @classmethod
    def create(cls, *, product_id: ProductId, sku: Sku, name: str, price: Money) -> Product:
        product = cls(product_id=product_id, sku=sku, name=name, price=price)
        product._record(
            ProductCreated(product_id=product_id, sku=sku, name=product.name, price=product.price)
        )
        return product

    @classmethod
    def create_kit(
        cls,
        *,
        product_id: ProductId,
        sku: Sku,
        name: str,
        price: Money,
        components: Sequence[KitComponent],
    ) -> Product:
        """Los componentes deben existir y no ser kits: eso lo verifica el caso de uso,
        que puede consultar los otros productos."""
        kit = cls(
            product_id=product_id,
            sku=sku,
            name=name,
            price=price,
            components=_validate_components(product_id, components),
        )
        kit._record(
            ProductCreated(
                product_id=product_id,
                sku=sku,
                name=kit.name,
                price=kit.price,
                components=kit.components,
            )
        )
        return kit

    def change_composition(self, components: Sequence[KitComponent]) -> None:
        if not self.is_kit:
            raise InvalidKitError(f"El producto {self.sku} no es un kit")
        new_components = _validate_components(self.product_id, components)
        if set(new_components) == set(self.components):
            return
        self.components = new_components
        self._record(KitCompositionChanged(product_id=self.product_id, components=new_components))

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
