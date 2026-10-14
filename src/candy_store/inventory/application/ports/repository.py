from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any

from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.application.unit_of_work import UnitOfWork
from candy_store.shared.domain.value_objects import ProductId


class InventoryRepository(ABC):
    @abstractmethod
    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        """Carga el agregado completo. La implementación debe aplicar bloqueo optimista
        con `InventoryItem.version` al confirmar."""

    @abstractmethod
    async def list_by_reservation(self, reference: StockReference) -> list[InventoryItem]:
        """Carga los agregados que tienen una reserva con esa referencia."""

    @abstractmethod
    async def add(self, item: InventoryItem) -> None: ...

    @abstractmethod
    async def keys_with_expired_reservations(
        self, now: datetime, limit: int
    ) -> list[tuple[ProductId, StockLocation]]:
        """Identifica agregados con reservas vencidas, sin cargarlos."""


class InventoryOperation(StrEnum):
    SALE_DEDUCTION = "sale_deduction"
    RESERVATION = "reservation"
    RESERVATION_CONFIRMATION = "reservation_confirmation"
    RESERVATION_RELEASE = "reservation_release"
    KIT_ASSEMBLY = "kit_assembly"


class OperationLog(ABC):
    """Registro de operaciones ya procesadas por referencia, para idempotencia.

    Se escribe en la misma transacción que el cambio de stock: o quedan las dos
    cosas o ninguna.
    """

    @abstractmethod
    async def get(
        self, reference: StockReference, operation: InventoryOperation
    ) -> dict[str, Any] | None: ...

    @abstractmethod
    async def add(
        self, reference: StockReference, operation: InventoryOperation, result: dict[str, Any]
    ) -> None: ...


class InventoryUnitOfWork(UnitOfWork):
    items: InventoryRepository
    operations: OperationLog
