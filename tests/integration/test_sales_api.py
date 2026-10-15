"""Flujos de venta de punta a punta por HTTP: catálogo + inventario + ventas con la
base real y el cableado real de main.py. Solo MercadoPago es un doble."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from candy_store.sales.domain.value_objects import OrderId
from candy_store.shared.domain.value_objects import Money
from tests.integration.conftest import Shop
from tests.unit.inventory.conftest import NOW

pytestmark = pytest.mark.integration

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


async def test_recent_orders_listing(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", store=10, warehouse=10)
    counter_ids = []
    for minutes in (1, 2):
        shop.clock.current = NOW + timedelta(minutes=minutes)
        order_id = str(uuid4())
        counter_ids.append(order_id)
        await shop.client.post(
            "/sales/counter-sales",
            json={
                "order_id": order_id,
                "items": [{"product_id": alfajor, "quantity": 1}],
                "payment_method": "cash",
            },
        )
    shop.clock.current = NOW + timedelta(minutes=3)
    online_id = (await start_checkout(shop, alfajor, 1))["order_id"]

    counter = await shop.client.get("/sales/orders", params={"channel": "counter"})
    everything = await shop.client.get("/sales/orders", params={"limit": 2})

    assert [o["order_id"] for o in counter.json()] == list(reversed(counter_ids))
    assert [o["order_id"] for o in everything.json()] == [online_id, counter_ids[1]]


async def test_online_purchase_paid_by_bank_transfer(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", warehouse=10)
    order_id = str(uuid4())

    checkout = await shop.client.post(
        "/sales/checkouts",
        json={
            "order_id": order_id,
            "items": [{"product_id": alfajor, "quantity": 4}],
            "customer_email": "cliente@example.com",
            "payment_method": "bank_transfer",
        },
    )

    assert checkout.status_code == 201, checkout.text
    body = checkout.json()
    assert (body["payment_method"], body["redirect_url"]) == ("bank_transfer", None)
    assert body["transfer"]["amount"] == "6000.00"
    assert body["transfer"]["reference"] == f"CANDY-{order_id[:8].upper()}"
    assert body["transfer"]["alias"]
    assert shop.payments.checkouts == []  # no pasa por Mercado Pago
    assert (await shop.stock(alfajor))["warehouse"]["reserved"] == 4

    confirmed = await shop.client.post(
        f"/sales/orders/{order_id}/transfer-confirmation", json={"bank_reference": "OP-555"}
    )

    assert confirmed.status_code == 200, confirmed.text
    assert (confirmed.json()["status"], confirmed.json()["payment_method"]) == (
        "paid",
        "bank_transfer",
    )
    warehouse = (await shop.stock(alfajor))["warehouse"]
    assert (warehouse["on_hand"], warehouse["reserved"]) == (6, 0)


async def test_transfer_hold_lasts_longer_than_mercadopago(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", warehouse=10)
    order_id = str(uuid4())
    await shop.client.post(
        "/sales/checkouts",
        json={
            "order_id": order_id,
            "items": [{"product_id": alfajor, "quantity": 1}],
            "payment_method": "bank_transfer",
        },
    )

    shop.clock.current = NOW + timedelta(hours=2)  # Mercado Pago ya habría vencido
    await shop.run_background_jobs()
    assert (await shop.order(order_id))["status"] == "awaiting_payment"

    shop.clock.current = NOW + timedelta(hours=25)
    await shop.run_background_jobs()
    order = await shop.order(order_id)
    assert (order["status"], order["cancellation_reason"]) == ("cancelled", "hold_expired")


async def test_online_checkout_rejects_counter_payment_methods(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500", warehouse=10)
    response = await shop.client.post(
        "/sales/checkouts",
        json={
            "order_id": str(uuid4()),
            "items": [{"product_id": alfajor, "quantity": 1}],
            "payment_method": "cash",
        },
    )
    assert response.status_code == 422
