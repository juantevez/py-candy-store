from dataclasses import dataclass
from decimal import Decimal

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.domain.errors import ProductNotFoundError
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import Money, ProductId


@dataclass(frozen=True, slots=True)
class UpdateProductCommand:
    """Solo se modifican los campos informados."""

    product_id: ProductId
    name: str | None = None
    price: Decimal | None = None
    active: bool | None = None


class UpdateProductUseCase:
    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, command: UpdateProductCommand) -> None:
        async with self._uow as uow:
            product = await uow.products.get(command.product_id)
            if product is None:
                raise ProductNotFoundError(f"No existe el producto {command.product_id}")
            if command.name is not None:
                product.rename(command.name)
            if command.price is not None:
                product.change_price(Money(command.price))
            if command.active is not None:
                product.set_active(command.active)
            await uow.commit()
