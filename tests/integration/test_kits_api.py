"""Kits pre-armados de punta a punta: receta en catálogo, armado en inventario y venta."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from candy_store.sales.domain.value_objects import OrderId
from candy_store.shared.domain.value_objects import Money
from candy_store.shared.infrastructure.outbox import OutboxMessage
from tests.integration.conftest import Shop
from tests.unit.inventory.conftest import TODAY

pytestmark = pytest.mark.integration


async def create_kit(shop: Shop, sku: str, price: str, components: dict[str, int]) -> Any:
    return await shop.client.post(
        "/catalog/products",
        json={
            "sku": sku,
            "name": f"Kit {sku}",
            "price": price,
            "components": [{"product_id": p, "quantity": q} for p, q in components.items()],
        },
    )


async def receive(shop: Shop, product_id: str, quantity: int, days: int, lot: str) -> str:
    response = await shop.client.post(
        "/inventory/batches",
        json={
            "product_id": product_id,
            "location": "warehouse",
            "lot_number": lot,
            "expiration": (TODAY + timedelta(days=days)).isoformat(),
            "quantity": quantity,
        },
    )
    assert response.status_code == 201, response.text
    batch_id: str = response.json()["batch_id"]
    return batch_id


async def assemble(shop: Shop, kit_id: str, quantity: int, assembly_id: str | None = None) -> Any:
    return await shop.client.post(
        "/inventory/kit-assemblies",
        json={
            "assembly_id": assembly_id or str(uuid4()),
            "kit_product_id": kit_id,
            "location": "warehouse",
            "quantity": quantity,
        },
    )


async def test_preassembled_kit_lifecycle(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    turron = await shop.product("TUR-1", "2500")
    alf_soon = await receive(shop, alfajor, 4, days=40, lot="ALF-A")
    await receive(shop, alfajor, 20, days=120, lot="ALF-B")
    await receive(shop, turron, 10, days=200, lot="TUR-A")
    kit = (await create_kit(shop, "KIT-NAV", "9900", {alfajor: 2, turron: 1})).json()
    assert kit["is_kit"] is True

    # Armado de 5 cajas en el depósito.
    assembly_id = str(uuid4())
    response = await assemble(shop, kit["product_id"], 5, assembly_id)
    retry = await assemble(shop, kit["product_id"], 5, assembly_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert retry.json() == body
    assert body["quantity"] == 5
    assert body["expiration"] == (TODAY + timedelta(days=40)).isoformat()
    by_product = {c["product_id"]: c for c in body["components"]}
    assert by_product[alfajor]["quantity"] == 10
    assert by_product[alfajor]["allocations"][0] == {
        "batch_id": alf_soon,
        "lot_number": "ALF-A",
        "expiration": (TODAY + timedelta(days=40)).isoformat(),
        "quantity": 4,
    }
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 14
    assert (await shop.stock(turron))["warehouse"]["on_hand"] == 5
    kit_stock = (await shop.stock(kit["product_id"]))["warehouse"]
    assert kit_stock["on_hand"] == 5
    assert kit_stock["batches"][0]["lot_number"] == body["lot_number"]

    # El kit se vende online como cualquier producto; los componentes no se tocan.
    checkout = await shop.client.post(
        "/sales/checkouts",
        json={
            "order_id": str(uuid4()),
            "items": [{"product_id": kit["product_id"], "quantity": 2}],
        },
    )
    assert checkout.status_code == 201, checkout.text
    order_id = checkout.json()["order_id"]
    shop.payments.register("777", OrderId.from_string(order_id), Money.of("19800"))
    assert await shop.notify_payment("777") == (200, {"outcome": "paid"})

    assert (await shop.stock(kit["product_id"]))["warehouse"]["on_hand"] == 3
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 14


async def test_assembly_traceability_is_published(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    turron = await shop.product("TUR-1", "2500")
    alf_lot = await receive(shop, alfajor, 10, days=60, lot="ALF-A")
    await receive(shop, turron, 10, days=60, lot="TUR-A")
    kit = (await create_kit(shop, "KIT-NAV", "9900", {alfajor: 2, turron: 1})).json()

    batch_id = (await assemble(shop, kit["product_id"], 3)).json()["batch_id"]

    async with shop.session_factory() as session:
        [message] = (
            await session.scalars(
                select(OutboxMessage).where(OutboxMessage.event_type == "KitsAssembled")
            )
        ).all()
    # Ante un retiro del lote ALF-A se puede saber en qué lote de kits terminó.
    assert message.payload["batch_id"] == batch_id
    alfajor_usage = next(c for c in message.payload["components"] if c["product_id"] == alfajor)
    assert alfajor_usage["allocations"][0]["batch_id"] == alf_lot


async def test_assembly_without_enough_components_moves_nothing(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    turron = await shop.product("TUR-1", "2500")
    await receive(shop, alfajor, 20, days=60, lot="ALF-A")
    await receive(shop, turron, 2, days=60, lot="TUR-A")
    kit = (await create_kit(shop, "KIT-NAV", "9900", {alfajor: 2, turron: 1})).json()

    response = await assemble(shop, kit["product_id"], 5)

    assert response.status_code == 409
    assert response.json()["code"] == "stock_shortage"
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 20
    assert "warehouse" not in await shop.stock(kit["product_id"])


async def test_recipe_change_only_affects_future_assemblies(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    turron = await shop.product("TUR-1", "2500")
    await receive(shop, alfajor, 30, days=60, lot="ALF-A")
    await receive(shop, turron, 30, days=60, lot="TUR-A")
    kit = (await create_kit(shop, "KIT-NAV", "9900", {alfajor: 2, turron: 1})).json()
    await assemble(shop, kit["product_id"], 2)  # 4 alfajores + 2 turrones

    changed = await shop.client.put(
        f"/catalog/products/{kit['product_id']}/components",
        json={"components": [{"product_id": alfajor, "quantity": 3}]},
    )
    await assemble(shop, kit["product_id"], 2)  # 6 alfajores, sin turrón

    assert changed.status_code == 200
    assert changed.json()["components"] == [{"product_id": alfajor, "quantity": 3}]
    assert (await shop.stock(alfajor))["warehouse"]["on_hand"] == 20
    assert (await shop.stock(turron))["warehouse"]["on_hand"] == 28
    assert (await shop.stock(kit["product_id"]))["warehouse"]["on_hand"] == 4


async def test_recipe_validations(shop: Shop) -> None:
    alfajor = await shop.product("ALF-1", "1500")
    kit = (await create_kit(shop, "KIT-A", "3000", {alfajor: 2})).json()

    nested = await create_kit(shop, "KIT-B", "5000", {kit["product_id"]: 1, alfajor: 1})
    missing = await create_kit(shop, "KIT-C", "5000", {str(uuid4()): 2})
    simple_recipe = await shop.client.put(
        f"/catalog/products/{alfajor}/components",
        json={"components": [{"product_id": kit["product_id"], "quantity": 2}]},
    )
    not_a_kit = await assemble(shop, alfajor, 1)

    assert (nested.status_code, nested.json()["code"]) == (422, "invalid_kit")
    assert "otros kits" in nested.json()["detail"]
    assert (missing.status_code, missing.json()["code"]) == (422, "invalid_kit")
    assert simple_recipe.status_code == 422
    assert (not_a_kit.status_code, not_a_kit.json()["code"]) == (422, "kit_recipe_not_found")
