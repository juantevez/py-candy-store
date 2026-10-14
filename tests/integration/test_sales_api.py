"""Flujos de venta de punta a punta por HTTP: catálogo + inventario + ventas con la
base real y el cableado real de main.py. Solo MercadoPago es un doble."""

import hashlib
import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.main import configure_services, create_app
from candy_store.sales.domain.value_objects import OrderId
from candy_store.shared.domain.value_objects import Money
from candy_store.shared.infrastructure.event_bus import EventBus
from candy_store.shared.infrastructure.outbox import OutboxRelay
from candy_store.shared.infrastructure.settings import Settings
from tests.fakes import FakePaymentGateway, FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "secreto-de-prueba"


@dataclass
class Shop:
    client: AsyncClient
    app: FastAPI
    payments: FakePaymentGateway
    clock: FixedClock
    session_factory: async_sessionmaker[AsyncSession]

    async def product(self, sku: str, price: str, *, store: int = 0, warehouse: int = 0) -> str:
        response = await self.client.post(
            "/catalog/products", json={"sku": sku, "name": f"Producto {sku}", "price": price}
        )
        assert response.status_code == 201, response.text
        product_id: str = response.json()["product_id"]
        for location, quantity in (("store_front", store), ("warehouse", warehouse)):
            if quantity:
                received = await self.client.post(
                    "/inventory/batches",
                    json={
                        "product_id": product_id,
                        "location": location,
                        "lot_number": "L1",
                        "expiration": (TODAY + timedelta(days=60)).isoformat(),
                        "quantity": quantity,
                    },
                )
                assert received.status_code == 201, received.text
        return product_id

    async def stock(self, product_id: str) -> dict[str, dict[str, int]]:
        body = (await self.client.get(f"/inventory/products/{product_id}/stock")).json()
        return {loc["location"]: loc for loc in body["locations"]}

    async def order(self, order_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/sales/orders/{order_id}")
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body

    async def notify_payment(
        self, payment_id: str, *, secret: str = WEBHOOK_SECRET
    ) -> tuple[int, dict[str, Any]]:
        request_id, ts = str(uuid4()), "1728918000000"
        manifest = f"id:{payment_id};request-id:{request_id};ts:{ts};"
        digest = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        response = await self.client.post(
            "/sales/webhooks/mercadopago",
            params={"data.id": payment_id, "type": "payment"},
            headers={"x-signature": f"ts={ts},v1={digest}", "x-request-id": request_id},
            json={"action": "payment.updated", "type": "payment", "data": {"id": payment_id}},
        )
        return response.status_code, response.json()

    async def run_background_jobs(self) -> None:
        """Lo que en producción hacen las tareas periódicas."""
        await ReleaseExpiredReservationsUseCase(
            SqlAlchemyInventoryUnitOfWork(self.session_factory), self.clock
        ).execute()
        await OutboxRelay(self.session_factory, self.app.state.event_bus).publish_pending()


@pytest.fixture
async def shop(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Shop]:
    settings = Settings(mercadopago_webhook_secret=WEBHOOK_SECRET)
    app = create_app(settings)
    payments = FakePaymentGateway()
    clock = FixedClock(NOW)
    # Mismo cableado que en producción, sin lifespan (sin tareas de fondo ni HTTP real).
    configure_services(
        app.state,
        settings=settings,
        session_factory=session_factory,
        clock=clock,
        event_bus=EventBus(),
        payment_gateway=payments,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Shop(client, app, payments, clock, session_factory)


# --- Mostrador -------------------------------------------------------------------


async def test_counter_sale(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", store=10, warehouse=50)
    chicle = await shop.product("CHI-1", "200", store=10)
    order_id = str(uuid4())
    sale = {
        "order_id": order_id,
        "items": [{"product_id": alfajor, "quantity": 2}, {"product_id": chicle, "quantity": 3}],
        "payment_method": "cash",
    }

    response = await shop.client.post("/sales/counter-sales", json=sale)
    retry = await shop.client.post("/sales/counter-sales", json=sale)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert Decimal(body["total"]) == Decimal("3600.00")
    assert body["payment_method"] == "cash"
    assert retry.json() == body
    assert (await shop.stock(alfajor))["store_front"]["on_hand"] == 8
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 50
    assert (await shop.stock(chicle))["store_front"]["on_hand"] == 7


async def test_counter_sale_without_stock_is_409_with_missing_items(shop: Shop) -> None:
    chicle = await shop.product("CHI-1", "200", store=1)
    order_id = str(uuid4())

    response = await shop.client.post(
        "/sales/counter-sales",
        json={
            "order_id": order_id,
            "items": [{"product_id": chicle, "quantity": 3}],
            "payment_method": "debit_card",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "out_of_stock"
    assert response.json()["items"] == [{"product_id": chicle, "requested": 3, "available": 1}]
    order = await shop.order(order_id)
    assert (order["status"], order["cancellation_reason"]) == ("cancelled", "out_of_stock")


async def test_price_comes_from_catalog_and_inactive_products_are_not_sold(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", store=10)
    await shop.client.patch(f"/catalog/products/{alfajor}", json={"price": "1800"})

    sale = await shop.client.post(
        "/sales/counter-sales",
        json={
            "order_id": str(uuid4()),
            "items": [{"product_id": alfajor, "quantity": 1}],
            "payment_method": "cash",
        },
    )
    assert Decimal(sale.json()["total"]) == Decimal("1800.00")

    await shop.client.patch(f"/catalog/products/{alfajor}", json={"active": False})
    blocked = await shop.client.post(
        "/sales/counter-sales",
        json={
            "order_id": str(uuid4()),
            "items": [{"product_id": alfajor, "quantity": 1}],
            "payment_method": "cash",
        },
    )
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "product_not_sellable"


# --- Checkout online + MercadoPago ---------------------------------------------------


async def start_checkout(shop: Shop, product_id: str, quantity: int) -> dict[str, Any]:
    response = await shop.client.post(
        "/sales/checkouts",
        json={
            "order_id": str(uuid4()),
            "items": [{"product_id": product_id, "quantity": quantity}],
            "customer_email": "cliente@example.com",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_online_checkout_and_approved_payment(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", store=5, warehouse=10)

    checkout = await start_checkout(shop, alfajor, 4)
    order_id = checkout["order_id"]

    assert checkout["redirect_url"] == f"https://mp.test/checkout/{order_id}"
    assert Decimal(checkout["total"]) == Decimal("6000.00")
    warehouse = (await shop.stock(alfajor))["warehouse"]
    assert (warehouse["reserved"], warehouse["available"]) == (4, 6)
    assert (await shop.order(order_id))["status"] == "awaiting_payment"

    shop.payments.register("555001", OrderId.from_string(order_id), Money.of("6000"))
    status, body = await shop.notify_payment("555001")
    duplicate_status, duplicate = await shop.notify_payment("555001")

    assert (status, body) == (200, {"outcome": "paid"})
    assert (duplicate_status, duplicate) == (200, {"outcome": "already_processed"})
    order = await shop.order(order_id)
    assert (order["status"], order["payment_method"]) == ("paid", "mercadopago")
    warehouse = (await shop.stock(alfajor))["warehouse"]
    assert (warehouse["on_hand"], warehouse["reserved"]) == (6, 0)
    assert (await shop.stock(alfajor))["store_front"]["on_hand"] == 5


async def test_webhook_with_invalid_signature_is_rejected(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", warehouse=10)
    order_id = (await start_checkout(shop, alfajor, 1))["order_id"]
    shop.payments.register("555001", OrderId.from_string(order_id), Money.of("1500"))

    status, _ = await shop.notify_payment("555001", secret="clave-de-un-atacante")

    assert status == 401
    assert (await shop.order(order_id))["status"] == "awaiting_payment"


async def test_unpaid_checkout_is_cancelled_when_hold_expires_then_late_payment_is_refunded(
    shop: Shop,
) -> None:
    alfajor = await shop.product("ALF-1", "1500", warehouse=10)
    order_id = (await start_checkout(shop, alfajor, 4))["order_id"]

    shop.clock.current = NOW + timedelta(minutes=16)
    await shop.run_background_jobs()

    order = await shop.order(order_id)
    assert (order["status"], order["cancellation_reason"]) == ("cancelled", "hold_expired")
    assert (await shop.stock(alfajor))["warehouse"]["available"] == 10

    # Un pago en efectivo que se acredita tarde: no se entrega, hay que devolverlo.
    shop.payments.register("555002", OrderId.from_string(order_id), Money.of("6000"))
    status, body = await shop.notify_payment("555002")

    assert (status, body) == (200, {"outcome": "refund_required"})
    order = await shop.order(order_id)
    assert (order["status"], order["refund_reason"]) == ("refund_required", "order_cancelled")
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 10


async def test_payment_gateway_down_is_502_and_releases_stock(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", warehouse=10)
    shop.payments.fail_checkout = True
    order_id = str(uuid4())

    response = await shop.client.post(
        "/sales/checkouts",
        json={"order_id": order_id, "items": [{"product_id": alfajor, "quantity": 4}]},
    )

    assert response.status_code == 502
    assert response.json()["code"] == "payment_gateway"
    assert (await shop.stock(alfajor))["warehouse"]["available"] == 10
    assert (await shop.order(order_id))["cancellation_reason"] == "checkout_failed"


async def test_signature_is_required_even_for_non_payment_topics(shop: Shop) -> None:
    response = await shop.client.post(
        "/sales/webhooks/mercadopago",
        params={"data.id": "1", "type": "merchant_order"},
        headers={"x-signature": "ts=1,v1=x", "x-request-id": "r"},
    )
    assert response.status_code == 401


# --- Catálogo ----------------------------------------------------------------------


async def test_catalog_crud(shop: Shop) -> None:
    created = await shop.client.post(
        "/catalog/products", json={"sku": "alf-triple", "name": "Alfajor triple", "price": "1500"}
    )
    product_id = created.json()["product_id"]

    duplicate = await shop.client.post(
        "/catalog/products", json={"sku": "ALF-TRIPLE", "name": "Otro", "price": "1"}
    )
    updated = await shop.client.patch(
        f"/catalog/products/{product_id}", json={"price": "1750.5", "name": "Alfajor triple x3"}
    )
    listed = await shop.client.get("/catalog/products")

    assert created.status_code == 201
    assert created.json()["sku"] == "ALF-TRIPLE"
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate_sku"
    assert Decimal(updated.json()["price"]) == Decimal("1750.50")
    assert updated.json()["name"] == "Alfajor triple x3"
    assert [p["sku"] for p in listed.json()] == ["ALF-TRIPLE"]
    assert (await shop.client.get(f"/catalog/products/{uuid4()}")).status_code == 404
