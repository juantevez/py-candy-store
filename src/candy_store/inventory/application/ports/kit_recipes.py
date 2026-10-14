from abc import ABC, abstractmethod
from dataclasses import dataclass

from candy_store.inventory.application.dto import StockLine
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class KitRecipe:
    """Cuántas unidades de cada componente lleva UN kit."""

    kit_product_id: ProductId
    components: tuple[StockLine, ...]


class KitRecipeProvider(ABC):
    """Fuente de las recetas de kits (las define el catálogo)."""

    @abstractmethod
    async def get(self, kit_product_id: ProductId) -> KitRecipe | None:
        """None si el producto no existe o no es un kit."""
