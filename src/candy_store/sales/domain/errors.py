from candy_store.shared.domain.errors import DomainError


class InvalidOrderError(DomainError):
    pass


class EmptyOrderError(InvalidOrderError):
    pass


class InvalidOrderStateError(DomainError):
    """La operación no es válida en el estado actual de la orden."""


class PaymentAmountMismatchError(DomainError):
    pass


class OrderNotFoundError(DomainError):
    pass


class ProductNotSellableError(DomainError):
    """Producto inexistente o inactivo en el catálogo."""

    def __init__(self, product_ids: list[str]) -> None:
        super().__init__(f"Productos no disponibles para la venta: {', '.join(product_ids)}")
        self.product_ids = product_ids
