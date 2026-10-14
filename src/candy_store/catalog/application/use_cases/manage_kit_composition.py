from collections.abc import Sequence
from dataclasses import dataclass

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.application.use_cases._kits import ensure_valid_components
from candy_store.catalog.domain.errors import ProductNotFoundError
from candy_store.catalog.domain.value_objects import KitComponent
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class SetKitCompositionCommand:
    kit_id: ProductId
    components: Sequence[KitComponent]


class SetKitCompositionUseCase:
    """Cambia la receta de un kit. Solo afecta a los kits que se armen después:
    los ya armados conservan los componentes con que se armaron."""

    def __init__(self, uow: CatalogUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, command: SetKitCompositionCommand) -> None:
        async with self._uow as uow:
            kit = await uow.products.get(command.kit_id)
            if kit is None:
                raise ProductNotFoundError(f"No existe el producto {command.kit_id}")
            await ensure_valid_components(uow, command.components)
            kit.change_composition(command.components)
            await uow.commit()
