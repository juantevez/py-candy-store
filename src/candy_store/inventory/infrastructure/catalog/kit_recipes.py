"""Anti-corruption layer: recetas de kits de inventory sobre la API pública del catálogo."""

from candy_store.catalog import public as catalog
from candy_store.inventory.application.dto import StockLine
from candy_store.inventory.application.ports.kit_recipes import KitRecipe, KitRecipeProvider
from candy_store.shared.domain.value_objects import ProductId


class InProcessKitRecipes(KitRecipeProvider):
    def __init__(self, facade: catalog.CatalogFacade) -> None:
        self._catalog = facade

    async def get(self, kit_product_id: ProductId) -> KitRecipe | None:
        components = await self._catalog.get_kit_recipe(kit_product_id)
        if components is None:
            return None
        return KitRecipe(
            kit_product_id=kit_product_id,
            components=tuple(StockLine(c.product_id, c.quantity) for c in components),
        )
