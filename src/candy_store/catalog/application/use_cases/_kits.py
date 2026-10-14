from collections.abc import Sequence

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.domain.errors import InvalidKitError
from candy_store.catalog.domain.value_objects import KitComponent


async def ensure_valid_components(
    uow: CatalogUnitOfWork, components: Sequence[KitComponent]
) -> None:
    """Reglas de la receta que involucran a otros productos: los componentes
    tienen que existir y no pueden ser kits (sin kits anidados)."""
    ids = {c.product_id for c in components}
    found = {p.product_id: p for p in await uow.products.get_many(ids)}
    missing = sorted(str(pid) for pid in ids - found.keys())
    if missing:
        raise InvalidKitError(f"Componentes inexistentes: {', '.join(missing)}")
    nested = sorted(p.sku.value for p in found.values() if p.is_kit)
    if nested:
        raise InvalidKitError(f"Un kit no puede contener otros kits: {', '.join(nested)}")
