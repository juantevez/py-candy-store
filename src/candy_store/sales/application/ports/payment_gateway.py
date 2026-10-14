"""Pasarela de pagos online (interfaz para MercadoPago)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from candy_store.sales.domain.value_objects import CheckoutSession, OrderId
from candy_store.shared.domain.value_objects import Money, ProductId


class PaymentStatus(StrEnum):
    APPROVED = "approved"
    PENDING = "pending"  # incluye "en proceso" y pagos en efectivo aún no acreditados
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    CHARGED_BACK = "charged_back"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CheckoutItem:
    product_id: ProductId
    title: str
    quantity: int
    unit_price: Money


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    order_id: OrderId
    items: tuple[CheckoutItem, ...]
    payer_email: str | None
    # Después de esta fecha la pasarela no debe aceptar el pago (vence la reserva de stock).
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PaymentDetails:
    """Estado de un pago consultado a la pasarela (nunca se confía en el webhook)."""

    payment_id: str
    status: PaymentStatus
    order_id: OrderId | None
    amount: Money


class PaymentGatewayError(Exception):
    """La pasarela no respondió o rechazó la operación."""


class PaymentGateway(ABC):
    @abstractmethod
    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession: ...

    @abstractmethod
    async def get_payment(self, payment_id: str) -> PaymentDetails: ...
