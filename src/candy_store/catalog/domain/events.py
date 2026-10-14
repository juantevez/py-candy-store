from dataclasses import dataclass

from candy_store.catalog.domain.value_objects import Sku
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import Money, ProductId


@dataclass(frozen=True, kw_only=True)
class ProductCreated(DomainEvent):
    product_id: ProductId
    sku: Sku
    name: str
    price: Money


@dataclass(frozen=True, kw_only=True)
class ProductPriceChanged(DomainEvent):
    product_id: ProductId
    old_price: Money
    new_price: Money


@dataclass(frozen=True, kw_only=True)
class ProductAvailabilityChanged(DomainEvent):
    product_id: ProductId
    active: bool
