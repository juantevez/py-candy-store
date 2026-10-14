from datetime import date

from candy_store.inventory.application.dto import StockLine
from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.domain.errors import Shortage, StockShortageError
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import StockLocation


async def load_items_covering(
    uow: InventoryUnitOfWork,
    location: StockLocation,
    lines: tuple[StockLine, ...],
    today: date,
) -> list[InventoryItem]:
    """Carga el agregado de cada línea y verifica que todas tengan disponible.

    Falla con todos los faltantes juntos antes de modificar nada, así la
    operación es todo o nada.
    """
    items: list[InventoryItem] = []
    shortages: list[Shortage] = []
    for line in lines:
        item = await uow.items.get(line.product_id, location)
        available = item.available_quantity(today) if item else 0
        if item is None or line.quantity > available:
            shortages.append(Shortage(line.product_id, line.quantity, available))
        else:
            items.append(item)
    if shortages:
        raise StockShortageError(tuple(shortages))
    return items
