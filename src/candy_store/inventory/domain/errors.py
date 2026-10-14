from dataclasses import dataclass

from candy_store.shared.domain.errors import DomainError
from candy_store.shared.domain.value_objects import ProductId


class InvalidQuantityError(DomainError):
    pass


class InsufficientStockError(DomainError):
    def __init__(self, *, requested: int, available: int) -> None:
        super().__init__(f"Stock insuficiente: se pidieron {requested}, disponibles {available}")
        self.requested = requested
        self.available = available


class ExpiredBatchReceivedError(DomainError):
    pass


class DuplicateBatchError(DomainError):
    pass


class BatchNotFoundError(DomainError):
    pass


class DuplicateReservationError(DomainError):
    pass


class ReservationNotFoundError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class Shortage:
    product_id: ProductId
    requested: int
    available: int


class StockShortageError(DomainError):
    """Una operación de varias líneas no puede cubrirse completa. Informa todos los
    faltantes juntos para que el cliente pueda corregir el pedido de una vez."""

    def __init__(self, shortages: tuple[Shortage, ...]) -> None:
        detail = ", ".join(
            f"{s.product_id}: pedido {s.requested}, disponible {s.available}" for s in shortages
        )
        super().__init__(f"Stock insuficiente ({detail})")
        self.shortages = shortages
