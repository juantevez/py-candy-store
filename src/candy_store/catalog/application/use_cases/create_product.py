from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.application.use_cases._kits import ensure_valid_components
from candy_store.catalog.domain.errors import DuplicateSkuError
from candy_store.catalog.domain.model import Product
from candy_store.catalog.domain.value_objects import KitComponent, Sku
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import Money, ProductId


@dataclass(frozen=True, slots=True)
class CreateProductCommand:
    sku: str
    name: str
    price: Decimal
    # Con componentes, el producto es un kit pre-armado.
    components: Sequence[KitComponent] = field(default_factory=tuple)


class CreateProductUseCase:
    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, command: CreateProductCommand) -> ProductId:
        sku = Sku(command.sku)
        product_id = ProductId.new()
        async with self._uow as uow:
            if await uow.products.get_by_sku(sku) is not None:
                raise DuplicateSkuError(f"Ya existe un producto con SKU {sku}")
            if command.components:
                await ensure_valid_components(uow, command.components)
                product = Product.create_kit(
                    product_id=product_id,
                    sku=sku,
                    name=command.name,
                    price=Money(command.price),
                    components=command.components,
                )
            else:
                product = Product.create(
                    product_id=product_id, sku=sku, name=command.name, price=Money(command.price)
                )
            await uow.products.add(product)
            await uow.commit()
        return product.product_id
