from candy_store.shared.domain.errors import DomainError


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
