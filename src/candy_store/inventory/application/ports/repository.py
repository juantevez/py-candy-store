from abc import ABC, abstractmethod
from datetime import datetime

from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import ReservationId, StockLocation
from candy_store.shared.application.unit_of_work import UnitOfWork
from candy_store.shared.domain.value_objects import ProductId


class InventoryRepository(ABC):
    @abstractmethod
    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        """Carga el agregado completo. La implementación debe aplicar bloqueo optimista
        con `InventoryItem.version` al confirmar."""

    @abstractmethod
    async def get_by_reservation(self, reservation_id: ReservationId) -> InventoryItem | None:
        """Carga el agregado que contiene la reserva, si existe."""

    @abstractmethod
    async def add(self, item: InventoryItem) -> None: ...

    @abstractmethod
    async def keys_with_expired_reservations(
        self, now: datetime, limit: int
    ) -> list[tuple[ProductId, StockLocation]]:
        """Identifica agregados con reservas vencidas, sin cargarlos."""


class InventoryUnitOfWork(UnitOfWork):
    items: InventoryRepository
