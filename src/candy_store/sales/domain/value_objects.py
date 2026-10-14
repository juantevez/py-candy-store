from dataclasses import dataclass
from enum import StrEnum

from candy_store.sales.domain.errors import InvalidOrderError
from candy_store.shared.domain.value_objects import EntityId, Money, ProductId

_MAX_DESCRIPTION_LENGTH = 120


class OrderId(EntityId):
    """Identidad de una venta: ticket de mostrador u orden online."""


class SalesChannel(StrEnum):
    COUNTER = "counter"  # Mostrador (POS)
    ONLINE = "online"  # Tienda web


class OrderStatus(StrEnum):
    PLACED = "placed"  # Creada; el stock todavía no se tomó
    AWAITING_PAYMENT = "awaiting_payment"  # Online: stock apartado, esperando el pago
    COMPLETED = "completed"  # Mostrador: cobrada y entregada
    PAID = "paid"  # Online: pagada y con el stock descontado
    CANCELLED = "cancelled"
    REFUND_REQUIRED = "refund_required"  # Cobrada pero no se puede entregar: devolver el dinero


class PaymentMethod(StrEnum):
    CASH = "cash"
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    MERCADOPAGO = "mercadopago"
    BANK_TRANSFER = "bank_transfer"


class CancellationReason(StrEnum):
    OUT_OF_STOCK = "out_of_stock"
    CHECKOUT_FAILED = "checkout_failed"  # No se pudo iniciar el pago
    HOLD_EXPIRED = "hold_expired"  # Venció la reserva de stock sin pago aprobado


class RefundReason(StrEnum):
    STOCK_UNAVAILABLE = "stock_unavailable"  # Se aprobó el pago pero el stock ya no estaba
    ORDER_CANCELLED = "order_cancelled"  # El pago llegó después de cancelada la orden
    AMOUNT_MISMATCH = "amount_mismatch"  # El monto cobrado no coincide con el total


@dataclass(frozen=True, slots=True)
class OrderLine:
    """Línea de la orden con el precio congelado al momento de la venta."""

    product_id: ProductId
    description: str
    unit_price: Money
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidOrderError(f"La cantidad debe ser positiva: {self.quantity}")
        if self.unit_price.amount <= 0:
            raise InvalidOrderError(f"El precio unitario debe ser positivo: {self.unit_price}")
        if not self.description.strip() or len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise InvalidOrderError("Descripción de línea inválida")

    @property
    def subtotal(self) -> Money:
        return self.unit_price * self.quantity


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """Sesión de pago creada en la pasarela (ej. preferencia de MercadoPago)."""

    checkout_id: str
    redirect_url: str
