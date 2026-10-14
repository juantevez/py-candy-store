"""Compras de punta a punta: proveedor, orden de compra, recepción de remitos y el
ingreso asincrónico del stock en inventory (vía outbox)."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import update

from candy_store.shared.infrastructure.outbox import OutboxMessage, OutboxRelay
from tests.integration.conftest import Shop
from tests.unit.inventory.conftest import TODAY

pytestmark = pytest.mark.integration


async def supplier(shop: Shop, cuit: str = "30-71234567-1") -> str:
    response = await shop.client.post(
        "/purchasing/suppliers", json={"name": "Distribuidora Golosinas SA", "cuit": cuit}
    )
    assert response.status_code == 201, response.text
    supplier_id: str = response.json()["supplier_id"]
    return supplier_id


async def purchase_order(shop: Shop, supplier_id: str, lines: dict[str, int]) -> dict[str, Any]:
    response = await shop.client.post(
        "/purchasing/orders",
        json={
            "supplier_id": supplier_id,
            "lines": [
                {"product_id": pid, "quantity": qty, "unit_cost": "850.50"}
                for pid, qty in lines.items()
            ],
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def remittance(order_id: str, number: str, *lines: tuple[str, int, str, int]) -> dict[str, Any]:
    """lines: (producto, cantidad, lote, días hasta el vencimiento)"""
    return {
        "purchase_order_id": order_id,
        "remittance_number": number,
        "lines": [
            {
                "product_id": pid,
                "quantity": qty,
                "lot_number": lot,
                "expiration": (TODAY + timedelta(days=days)).isoformat(),
            }
            for pid, qty, lot, days in lines
        ],
    }


async def test_purchase_order_received_in_two_remittances(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    turron = await shop.product("TUR-1", "2500")
    supplier_id = await supplier(shop)
    order = await purchase_order(shop, supplier_id, {alfajor: 100, turron: 20})
    order_id = order["purchase_order_id"]
    assert (order["status"], order["total_cost"]) == ("open", "102060.00")

    # Primer remito: llega parte de los alfajores, en dos lotes, y todos los turrones.
    first = await shop.client.post(
        "/purchasing/remittances",
        json=remittance(
            order_id,
            "0001-00000123",
            (alfajor, 60, "ALF-A", 90),
            (alfajor, 20, "ALF-B", 120),
            (turron, 20, "TUR-A", 200),
        ),
    )
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "partially_received"
    pending = {line["product_id"]: line["pending"] for line in first.json()["lines"]}
    assert pending == {alfajor: 20, turron: 0}

    # El stock ingresa cuando inventory procesa el evento del outbox.
    assert "warehouse" not in await shop.stock(alfajor)
    await shop.run_background_jobs()
    warehouse = (await shop.stock(alfajor))["warehouse"]
    assert warehouse["on_hand"] == 80
    assert [b["lot_number"] for b in warehouse["batches"]] == ["ALF-A", "ALF-B"]
    assert (await shop.stock(turron))["warehouse"]["on_hand"] == 20

    # Segundo remito completa la orden.
    second = await shop.client.post(
        "/purchasing/remittances",
        json=remittance(order_id, "0001-00000124", (alfajor, 20, "ALF-C", 100)),
    )
    await shop.run_background_jobs()

    assert second.json()["status"] == "received"
    assert [r["remittance_number"] for r in second.json()["receipts"]] == [
        "00001-00000123",
        "00001-00000124",
    ]
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 100


async def test_remittance_not_matching_the_order_is_rejected(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    not_ordered = await shop.product("CHI-1", "200")
    order_id = (await purchase_order(shop, await supplier(shop), {alfajor: 10}))[
        "purchase_order_id"
    ]

    response = await shop.client.post(
        "/purchasing/remittances",
        json=remittance(
            order_id, "0001-00000001", (alfajor, 12, "A", 90), (not_ordered, 5, "C", 90)
        ),
    )
    await shop.run_background_jobs()

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "remittance_mismatch"
    assert {(d["product_id"], d["pending"], d["delivered"]) for d in body["discrepancies"]} == {
        (alfajor, 10, 12),
        (not_ordered, 0, 5),
    }
    assert "warehouse" not in await shop.stock(alfajor)
    order = (await shop.client.get(f"/purchasing/orders/{order_id}")).json()
    assert (order["status"], order["receipts"]) == ("open", [])


async def test_expired_goods_are_rejected(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    order_id = (await purchase_order(shop, await supplier(shop), {alfajor: 10}))[
        "purchase_order_id"
    ]

    response = await shop.client.post(
        "/purchasing/remittances",
        json=remittance(order_id, "0001-00000001", (alfajor, 10, "A", -1)),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_remittance"


async def test_same_remittance_twice_is_registered_once(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    supplier_id = await supplier(shop)
    order_id = (await purchase_order(shop, supplier_id, {alfajor: 100}))["purchase_order_id"]
    body = remittance(order_id, "0001-00000777", (alfajor, 30, "A", 90))

    first = await shop.client.post("/purchasing/remittances", json=body)
    retry = await shop.client.post("/purchasing/remittances", json=body)
    await shop.run_background_jobs()

    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert len(retry.json()["receipts"]) == 1
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 30

    other_order = (await purchase_order(shop, supplier_id, {alfajor: 5}))["purchase_order_id"]
    reused = await shop.client.post(
        "/purchasing/remittances",
        json=remittance(other_order, "00001-00000777", (alfajor, 5, "B", 90)),
    )
    assert (reused.status_code, reused.json()["code"]) == (409, "duplicate_remittance")


async def test_redelivered_event_does_not_duplicate_stock(shop: Shop) -> None:
    """El outbox entrega al menos una vez: simular que el evento llega dos veces."""
    alfajor = await shop.product("ALF-1", "1500")
    order_id = (await purchase_order(shop, await supplier(shop), {alfajor: 10}))[
        "purchase_order_id"
    ]
    await shop.client.post(
        "/purchasing/remittances",
        json=remittance(order_id, "0001-00000001", (alfajor, 10, "A", 90)),
    )
    await shop.run_background_jobs()

    async with shop.session_factory() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.event_type == "RemittanceReceived")
            .values(published_at=None)
        )
        await session.commit()
    await OutboxRelay(shop.session_factory, shop.app.state.event_bus).publish_pending()

    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 10


async def test_order_rules(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    supplier_id = await supplier(shop)
    kit = await shop.client.post(
        "/catalog/products",
        json={
            "sku": "KIT-1",
            "name": "Kit",
            "price": "5000",
            "components": [{"product_id": alfajor, "quantity": 2}],
        },
    )
    kit_id = kit.json()["product_id"]

    kit_order = await shop.client.post(
        "/purchasing/orders",
        json={
            "supplier_id": supplier_id,
            "lines": [{"product_id": kit_id, "quantity": 1, "unit_cost": "100"}],
        },
    )
    unknown_supplier = await shop.client.post(
        "/purchasing/orders",
        json={
            "supplier_id": str(uuid4()),
            "lines": [{"product_id": alfajor, "quantity": 1, "unit_cost": "100"}],
        },
    )
    duplicate_supplier = await shop.client.post(
        "/purchasing/suppliers", json={"name": "Otra", "cuit": "30712345671"}
    )
    invalid_cuit = await shop.client.post(
        "/purchasing/suppliers", json={"name": "Otra", "cuit": "30-71234567-2"}
    )

    assert (kit_order.status_code, kit_order.json()["code"]) == (422, "product_not_purchasable")
    assert unknown_supplier.status_code == 404
    assert (duplicate_supplier.status_code, duplicate_supplier.json()["code"]) == (
        409,
        "duplicate_supplier",
    )
    assert (invalid_cuit.status_code, invalid_cuit.json()["code"]) == (422, "invalid_supplier")


async def test_cancel_and_close(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    supplier_id = await supplier(shop)
    to_cancel = (await purchase_order(shop, supplier_id, {alfajor: 10}))["purchase_order_id"]
    to_close = (await purchase_order(shop, supplier_id, {alfajor: 10}))["purchase_order_id"]
    await shop.client.post(
        "/purchasing/remittances", json=remittance(to_close, "0001-00000001", (alfajor, 6, "A", 90))
    )

    cancelled = await shop.client.post(f"/purchasing/orders/{to_cancel}/cancellation")
    cannot_cancel = await shop.client.post(f"/purchasing/orders/{to_close}/cancellation")
    closed = await shop.client.post(f"/purchasing/orders/{to_close}/closure")
    late = await shop.client.post(
        "/purchasing/remittances", json=remittance(to_close, "0001-00000002", (alfajor, 4, "B", 90))
    )

    assert cancelled.json()["status"] == "cancelled"
    assert cannot_cancel.status_code == 409
    assert closed.json()["status"] == "closed"
    assert late.status_code == 409
