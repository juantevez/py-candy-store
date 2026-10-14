"""Adaptador concreto de MercadoPago (Checkout Pro) sobre su API REST.

Se usa httpx asíncrono en lugar del SDK oficial porque el SDK es sincrónico y
bloquearía el event loop de la aplicación en cada llamada.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from candy_store.sales.application.ports.payment_gateway import (
    CheckoutRequest,
    PaymentDetails,
    PaymentGateway,
    PaymentGatewayError,
    PaymentStatus,
)
from candy_store.sales.domain.value_objects import CheckoutSession, OrderId
from candy_store.shared.domain.value_objects import Money

_STATUS = {
    "approved": PaymentStatus.APPROVED,
    "authorized": PaymentStatus.PENDING,
    "pending": PaymentStatus.PENDING,
    "in_process": PaymentStatus.PENDING,
    "in_mediation": PaymentStatus.PENDING,
    "rejected": PaymentStatus.REJECTED,
    "cancelled": PaymentStatus.CANCELLED,
    "refunded": PaymentStatus.REFUNDED,
    "charged_back": PaymentStatus.CHARGED_BACK,
}


@dataclass(frozen=True, slots=True)
class MercadoPagoConfig:
    access_token: str
    notification_url: str
    success_url: str
    failure_url: str
    pending_url: str
    statement_descriptor: str = "CANDY STORE"


class MercadoPagoGateway(PaymentGateway):
    def __init__(self, client: httpx.AsyncClient, config: MercadoPagoConfig) -> None:
        """`client` debe tener `base_url` apuntando a la API (https://api.mercadopago.com)."""
        self._client = client
        self._config = config

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        expires_at = _mp_datetime(request.expires_at)
        body: dict[str, Any] = {
            "external_reference": str(request.order_id),
            "items": [
                {
                    "id": str(item.product_id),
                    "title": item.title,
                    "quantity": item.quantity,
                    "unit_price": float(item.unit_price.amount),
                    "currency_id": item.unit_price.currency,
                }
                for item in request.items
            ],
            "notification_url": self._config.notification_url,
            "back_urls": {
                "success": self._config.success_url,
                "failure": self._config.failure_url,
                "pending": self._config.pending_url,
            },
            "auto_return": "approved",
            "statement_descriptor": self._config.statement_descriptor,
            # No aceptar pagos después de que vence la reserva de stock.
            "expires": True,
            "expiration_date_to": expires_at,
            # Vencimiento de los cupones de pago en efectivo (Rapipago, Pago Fácil).
            "date_of_expiration": expires_at,
        }
        if request.payer_email:
            body["payer"] = {"email": request.payer_email}

        data = await self._request(
            "POST", "/checkout/preferences", json=body, idempotency_key=str(request.order_id)
        )
        try:
            return CheckoutSession(checkout_id=str(data["id"]), redirect_url=data["init_point"])
        except KeyError as exc:
            raise PaymentGatewayError(f"Respuesta inesperada de MercadoPago: falta {exc}") from exc

    async def get_payment(self, payment_id: str) -> PaymentDetails:
        if not payment_id.isdigit():
            raise PaymentGatewayError(f"Id de pago inválido: {payment_id!r}")
        data = await self._request("GET", f"/v1/payments/{payment_id}")
        try:
            return PaymentDetails(
                payment_id=str(data["id"]),
                status=_STATUS.get(data["status"], PaymentStatus.UNKNOWN),
                order_id=_order_id(data.get("external_reference")),
                amount=Money(Decimal(str(data["transaction_amount"])), data["currency_id"]),
            )
        except KeyError as exc:
            raise PaymentGatewayError(f"Respuesta inesperada de MercadoPago: falta {exc}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._config.access_token}"}
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise PaymentGatewayError(f"No se pudo contactar a MercadoPago: {exc}") from exc
        if response.is_error:
            raise PaymentGatewayError(
                f"MercadoPago respondió {response.status_code}: {response.text[:300]}"
            )
        body: dict[str, Any] = response.json()
        return body


def _mp_datetime(value: datetime) -> str:
    """Formato que espera MercadoPago: ISO 8601 con milisegundos y zona horaria."""
    if value.tzinfo is None:
        raise ValueError("MercadoPago requiere fechas con zona horaria")
    return value.isoformat(timespec="milliseconds")


def _order_id(external_reference: object) -> OrderId | None:
    if not isinstance(external_reference, str):
        return None
    try:
        return OrderId(UUID(external_reference))
    except ValueError:
        return None


def verify_webhook_signature(
    *, secret: str, x_signature: str | None, x_request_id: str | None, data_id: str | None
) -> bool:
    """Valida el header `x-signature` de una notificación de MercadoPago.

    El header tiene la forma `ts=<timestamp>,v1=<hmac>`. El HMAC-SHA256 (con la
    clave secreta del webhook) se calcula sobre el manifiesto
    `id:<data.id>;request-id:<x-request-id>;ts:<ts>;`, omitiendo las partes
    ausentes. Si `data.id` es alfanumérico va en minúsculas.
    """
    if not secret or not x_signature:
        return False
    parts = dict(part.strip().split("=", 1) for part in x_signature.split(",") if "=" in part)
    ts, received = parts.get("ts"), parts.get("v1")
    if not ts or not received:
        return False

    manifest = ""
    if data_id:
        manifest += f"id:{data_id.lower() if data_id.isalnum() else data_id};"
    if x_request_id:
        manifest += f"request-id:{x_request_id};"
    manifest += f"ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
