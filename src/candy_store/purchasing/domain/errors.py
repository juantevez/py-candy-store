from dataclasses import dataclass

from candy_store.shared.domain.errors import DomainError
from candy_store.shared.domain.value_objects import ProductId


class InvalidSupplierError(DomainError):
    pass


class DuplicateSupplierError(DomainError):
    pass


class SupplierNotFoundError(DomainError):
    pass


class InvalidPurchaseOrderError(DomainError):
    pass


class PurchaseOrderNotFoundError(DomainError):
    pass


class InvalidPurchaseOrderStateError(DomainError):
    pass


class InvalidRemittanceError(DomainError):
    pass


class DuplicateRemittanceError(DomainError):
    pass


class ProductNotPurchasableError(DomainError):
    """Producto inexistente en el catálogo o que es un kit (los kits se arman, no se compran)."""

    def __init__(self, product_ids: list[str]) -> None:
        super().__init__(f"Productos que no se pueden comprar: {', '.join(product_ids)}")
        self.product_ids = product_ids


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """Diferencia entre lo que trae el remito y lo pendiente en la orden de compra."""

    product_id: ProductId
    pending: int
    delivered: int


class RemittanceMismatchError(DomainError):
    """El remito trae productos no pedidos o más de lo pendiente. Se rechaza completo
    para que el depósito devuelva el excedente o se corrija la orden."""

    def __init__(self, discrepancies: tuple[Discrepancy, ...]) -> None:
        detail = ", ".join(
            f"{d.product_id}: pendiente {d.pending}, entregado {d.delivered}" for d in discrepancies
        )
        super().__init__(f"El remito no coincide con la orden de compra ({detail})")
        self.discrepancies = discrepancies
