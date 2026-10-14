from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from candy_store.shared.domain.value_objects import EntityId


class BatchId(EntityId):
    pass


class StockLocation(StrEnum):
    STORE_FRONT = "store_front"  # Salón de ventas
    WAREHOUSE = "warehouse"  # Depósito


class AdjustmentReason(StrEnum):
    SHRINKAGE = "shrinkage"  # Merma
    BREAKAGE = "breakage"  # Rotura
    EXPIRED = "expired"  # Baja por vencimiento
    AUDIT = "audit"  # Corrección por conteo físico


_MAX_LOT_NUMBER_LENGTH = 64
_MAX_REFERENCE_LENGTH = 64


@dataclass(frozen=True, slots=True)
class StockReference:
    """Referencia externa de la operación que mueve stock (ej. id de venta u orden).

    Identifica las reservas de una orden, hace idempotentes las operaciones y deja
    trazabilidad de qué lotes salieron por cada venta.
    """

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not normalized:
            raise ValueError("La referencia no puede estar vacía")
        if len(normalized) > _MAX_REFERENCE_LENGTH:
            raise ValueError(f"La referencia excede {_MAX_REFERENCE_LENGTH} caracteres")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class LotNumber:
    """Número de lote de fabricación impreso en el envase."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip().upper()
        if not normalized:
            raise ValueError("El número de lote no puede estar vacío")
        if len(normalized) > _MAX_LOT_NUMBER_LENGTH:
            raise ValueError(f"El número de lote excede {_MAX_LOT_NUMBER_LENGTH} caracteres")
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class ExpirationDate:
    """Fecha de vencimiento. El producto es vendible hasta ese día inclusive."""

    value: date

    def is_expired(self, today: date) -> bool:
        return self.value < today

    def days_until(self, today: date) -> int:
        return (self.value - today).days


@dataclass(frozen=True, slots=True)
class BatchAllocation:
    """Cuánto se toma de un lote concreto para cubrir una salida de stock."""

    batch_id: BatchId
    lot_number: LotNumber
    expiration: ExpirationDate
    quantity: int
