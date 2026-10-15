"""Consultas de lectura de stock (read model): listados para pantallas, sin cargar
los agregados completos."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class StockLevel:
    product_id: ProductId
    location: StockLocation
    on_hand: int
    sellable: int  # sin contar lotes vencidos
    reserved: int
    next_expiration: date | None  # vencimiento más próximo entre los lotes vendibles

    @property
    def available(self) -> int:
        return max(0, self.sellable - self.reserved)


class StockQueries(ABC):
    @abstractmethod
    async def levels_at(self, location: StockLocation, today: date) -> list[StockLevel]:
        """Stock de todos los productos con movimientos en la ubicación.

        Debe coincidir con `InventoryItem.available_quantity` (misma regla de
        vendible = no vencido, disponible = vendible - reservado).
        """
