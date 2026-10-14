"""Adaptador de MercadoPago contra un servidor HTTP simulado (httpx.MockTransport)."""

import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from candy_store.sales.application.ports.payment_gateway import (
    CheckoutItem,
    CheckoutRequest,
    PaymentGatewayError,
    PaymentStatus,
)
from candy_store.sales.domain.value_objects import OrderId
from candy_store.sales.infrastructure.external.mercadopago import (
    MercadoPagoConfig,
    MercadoPagoGateway,
    verify_webhook_signature,
)
from candy_store.shared.domain.value_objects import Money, ProductId
from candy_store.shared.infrastructure.clock import STORE_TIMEZONE

CONFIG = MercadoPagoConfig(
    access_token="TEST-token",
    notification_url="https://api.tienda.test/sales/webhooks/mercadopago",
    success_url="https://tienda.test/checkout/exito",
    failure_url="https://tienda.test/checkout/error",
    pending_url="https://tienda.test/checkout/pendiente",
)

type Handler = Callable[[httpx.Request], httpx.Response]


def gateway(handler: Handler) -> MercadoPagoGateway:
    client = httpx.AsyncClient(
        base_url="https://api.mercadopago.com", transport=httpx.MockTransport(handler)
    )
    return MercadoPagoGateway(client, CONFIG)


def checkout_request(order_id: OrderId) -> CheckoutRequest:
    return CheckoutRequest(
        order_id=order_id,
        items=(
            CheckoutItem(ProductId.new(), "Alfajor triple", 2, Money.of("1500.50")),
            CheckoutItem(ProductId.new(), "Chicle", 1, Money.of("200")),
        ),
        payer_email="cliente@example.com",
        expires_at=datetime(2026, 10, 14, 15, 15, tzinfo=UTC).astimezone(STORE_TIMEZONE),
    )


async def test_create_checkout_sends_preference_and_returns_redirect() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            201, json={"id": "123-abc", "init_point": "https://www.mercadopago.com.ar/checkout/x"}
        )

    order_id = OrderId.new()
    session = await gateway(handler).create_checkout(checkout_request(order_id))

    assert session.checkout_id == "123-abc"
    assert session.redirect_url == "https://www.mercadopago.com.ar/checkout/x"
    [request] = captured
    assert request.method == "POST"
    assert request.url.path == "/checkout/preferences"
    assert request.headers["Authorization"] == "Bearer TEST-token"
    assert request.headers["X-Idempotency-Key"] == str(order_id)
    body = json.loads(request.content)
    assert body["external_reference"] == str(order_id)
    assert body["notification_url"] == CONFIG.notification_url
    assert body["auto_return"] == "approved"
    assert body["expires"] is True
    assert body["expiration_date_to"] == "2026-10-14T12:15:00.000-03:00"
    assert body["date_of_expiration"] == body["expiration_date_to"]
    assert body["payer"] == {"email": "cliente@example.com"}
    assert [
        (i["title"], i["quantity"], i["unit_price"], i["currency_id"]) for i in body["items"]
    ] == [
        ("Alfajor triple", 2, 1500.5, "ARS"),
        ("Chicle", 1, 200.0, "ARS"),
    ]


@pytest.mark.parametrize(
    ("mp_status", "expected"),
    [
        ("approved", PaymentStatus.APPROVED),
        ("in_process", PaymentStatus.PENDING),
        ("rejected", PaymentStatus.REJECTED),
        ("refunded", PaymentStatus.REFUNDED),
        ("algo_nuevo", PaymentStatus.UNKNOWN),
    ],
)
async def test_get_payment_maps_status_reference_and_amount(
    mp_status: str, expected: PaymentStatus
) -> None:
    order_id = OrderId.new()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/payments/987654"
        return httpx.Response(
            200,
            json={
                "id": 987654,
                "status": mp_status,
                "external_reference": str(order_id),
                "transaction_amount": 3201.0,
                "currency_id": "ARS",
            },
        )

    payment = await gateway(handler).get_payment("987654")

    assert payment.payment_id == "987654"
    assert payment.status is expected
    assert payment.order_id == order_id
    assert payment.amount == Money(Decimal("3201.00"))


async def test_foreign_external_reference_is_not_an_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 1,
                "status": "approved",
                "external_reference": "otro-sistema-42",
                "transaction_amount": 10,
                "currency_id": "ARS",
            },
        )

    assert (await gateway(handler).get_payment("1")).order_id is None


async def test_http_errors_become_gateway_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid access token"})

    with pytest.raises(PaymentGatewayError, match="401"):
        await gateway(handler).create_checkout(checkout_request(OrderId.new()))


async def test_network_errors_become_gateway_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    with pytest.raises(PaymentGatewayError, match="contactar"):
        await gateway(handler).get_payment("1")


async def test_payment_id_must_be_numeric() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debería llamar a la API")

    with pytest.raises(PaymentGatewayError):
        await gateway(handler).get_payment("../../v1/users/me")


async def test_unexpected_response_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "x"})

    with pytest.raises(PaymentGatewayError, match="init_point"):
        await gateway(handler).create_checkout(checkout_request(OrderId.new()))


async def test_naive_expiration_is_rejected() -> None:
    request = checkout_request(OrderId.new())
    naive = CheckoutRequest(
        order_id=request.order_id,
        items=request.items,
        payer_email=None,
        expires_at=datetime(2026, 10, 14, 15, 15),  # noqa: DTZ001
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debería llamar a la API")

    with pytest.raises(ValueError, match="zona horaria"):
        await gateway(handler).create_checkout(naive)


# --- Firma del webhook -----------------------------------------------------------

SECRET = "clave-secreta-webhook"


def sign(data_id: str, request_id: str, ts: str = "1728918000000", secret: str = SECRET) -> str:
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    digest = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return f"ts={ts},v1={digest}"


def test_valid_signature() -> None:
    assert verify_webhook_signature(
        secret=SECRET, x_signature=sign("123456", "req-1"), x_request_id="req-1", data_id="123456"
    )


def test_alphanumeric_data_id_is_lowercased() -> None:
    assert verify_webhook_signature(
        secret=SECRET, x_signature=sign("abc123", "req-1"), x_request_id="req-1", data_id="ABC123"
    )


@pytest.mark.parametrize(
    ("x_signature", "request_id", "data_id"),
    [
        (sign("123456", "req-1", secret="otra-clave"), "req-1", "123456"),  # otra clave
        (sign("123456", "req-1"), "req-1", "999999"),  # otro pago
        (sign("123456", "req-1"), "req-2", "123456"),  # otro request
        ("ts=1,v1=deadbeef", "req-1", "123456"),
        ("basura", "req-1", "123456"),
        (None, "req-1", "123456"),
    ],
)
def test_invalid_signatures(x_signature: str | None, request_id: str, data_id: str) -> None:
    assert not verify_webhook_signature(
        secret=SECRET, x_signature=x_signature, x_request_id=request_id, data_id=data_id
    )


def test_without_configured_secret_everything_is_rejected() -> None:
    assert not verify_webhook_signature(
        secret="", x_signature=sign("1", "r", secret=""), x_request_id="r", data_id="1"
    )
