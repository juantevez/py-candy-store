from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.inventory.public import InventoryFacade, StockRequestLine
from candy_store.main import create_app
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.settings import Settings
from tests.fakes import FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    # ASGITransport no ejecuta el lifespan: se inyecta el estado a mano, sin
    # tareas de fondo, para que los tests sean deterministas.
    settings = Settings()
    app = create_app(settings)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.clock = FixedClock(NOW)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def batch_body(product_id: str, *, quantity: int = 10, expires_in_days: int = 30) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "location": "store_front",
        "lot_number": f"lote-{expires_in_days}",
        "expiration": (TODAY + timedelta(days=expires_in_days)).isoformat(),
        "quantity": quantity,
    }


async def receive(client: AsyncClient, product_id: str, **kwargs: int) -> str:
    response = await client.post("/inventory/batches", json=batch_body(product_id, **kwargs))
    assert response.status_code == 201, response.text
    batch_id: str = response.json()["batch_id"]
    return batch_id


async def stock_of(client: AsyncClient, product_id: str) -> dict[str, Any]:
    response = await client.get(f"/inventory/products/{product_id}/stock")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_received_batches_show_up_in_stock_by_expiration(client: AsyncClient) -> None:
    product_id = str(uuid4())
    later = await receive(client, product_id, quantity=10, expires_in_days=60)
    sooner = await receive(client, product_id, quantity=3, expires_in_days=5)

    summary = await stock_of(client, product_id)

    assert summary["total_available"] == 13
    [location] = summary["locations"]
    assert location["location"] == "store_front"
    assert [b["batch_id"] for b in location["batches"]] == [sooner, later]
    assert location["batches"][0] == {
        "batch_id": sooner,
        "lot_number": "LOTE-5",
        "expiration": "2026-10-19",
        "quantity_on_hand": 3,
        "expired": False,
    }


async def test_losses_and_audit_counts(client: AsyncClient) -> None:
    product_id = str(uuid4())
    batch_id = await receive(client, product_id, quantity=10)
    target = {"product_id": product_id, "location": "store_front", "batch_id": batch_id}

    loss = await client.post(
        "/inventory/losses", json={**target, "quantity": 2, "reason": "breakage"}
    )
    audit = await client.post("/inventory/audit-counts", json={**target, "counted_quantity": 9})

    assert loss.status_code == 204
    assert audit.json() == {"delta": 1}
    assert (await stock_of(client, product_id))["total_available"] == 9


async def test_loss_bigger_than_batch_is_409_with_context(client: AsyncClient) -> None:
    product_id = str(uuid4())
    batch_id = await receive(client, product_id, quantity=2)

    response = await client.post(
        "/inventory/losses",
        json={
            "product_id": product_id,
            "location": "store_front",
            "batch_id": batch_id,
            "quantity": 5,
            "reason": "shrinkage",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "insufficient_stock",
        "detail": "Stock insuficiente: se pidieron 5, disponibles 2",
        "requested": 5,
        "available": 2,
    }


async def test_unknown_batch_is_404(client: AsyncClient) -> None:
    response = await client.post(
        "/inventory/losses",
        json={
            "product_id": str(uuid4()),
            "location": "store_front",
            "batch_id": str(uuid4()),
            "quantity": 1,
            "reason": "shrinkage",
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "batch_not_found"


async def test_receiving_expired_batch_is_422(client: AsyncClient) -> None:
    response = await client.post(
        "/inventory/batches", json=batch_body(str(uuid4()), expires_in_days=-1)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "expired_batch_received"


@pytest.mark.parametrize(
    "override",
    [
        {"product_id": "no-uuid"},
        {"location": "garage"},
        {"quantity": 0},
        {"lot_number": "   "},
        {"expiration": "mañana"},
        {"extra": True},
    ],
)
async def test_invalid_payloads_are_rejected(client: AsyncClient, override: dict[str, Any]) -> None:
    response = await client.post(
        "/inventory/batches", json={**batch_body(str(uuid4())), **override}
    )
    assert response.status_code == 422


async def test_audit_reason_is_not_accepted_as_loss(client: AsyncClient) -> None:
    response = await client.post(
        "/inventory/losses",
        json={
            "product_id": str(uuid4()),
            "location": "store_front",
            "batch_id": str(uuid4()),
            "quantity": 1,
            "reason": "audit",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize("path", ["/inventory/deductions", "/inventory/reservations"])
async def test_stock_outflows_are_not_exposed_over_http(client: AsyncClient, path: str) -> None:
    """Las salidas de stock solo ocurren a través de sales (inventory.public)."""
    response = await client.post(path, json={})
    assert response.status_code == 404


async def test_location_stock_listing_matches_aggregate_rules(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """El read model en SQL aplica las mismas reglas que el agregado: lotes vencidos
    no son vendibles y lo reservado no está disponible."""
    with_expired = str(uuid4())
    reserved = str(uuid4())
    other_location = str(uuid4())
    await receive(client, with_expired, quantity=10, expires_in_days=30)
    await receive(client, reserved, quantity=8, expires_in_days=5)
    await receive(client, reserved, quantity=4, expires_in_days=60)
    await client.post(
        "/inventory/batches",
        json={**batch_body(other_location), "location": "warehouse"},
    )
    # Lote ya vencido: se inserta "en el pasado" con un reloj anterior.
    async with SqlAlchemyInventoryUnitOfWork(session_factory) as uow:
        item = await uow.items.get(ProductId.from_string(with_expired), StockLocation.STORE_FRONT)
        assert item is not None
        item.receive_batch(
            batch_id=BatchId.new(),
            lot_number=LotNumber("VIEJO"),
            expiration=ExpirationDate(TODAY - timedelta(days=1)),
            quantity=5,
            received_at=NOW,
            today=TODAY - timedelta(days=10),
        )
        await uow.commit()
    await InventoryFacade(
        lambda: SqlAlchemyInventoryUnitOfWork(session_factory),
        FixedClock(NOW),
        timedelta(minutes=15),
    ).reserve(
        "ORD-1", StockLocation.STORE_FRONT, [StockRequestLine(ProductId.from_string(reserved), 3)]
    )

    response = await client.get("/inventory/stock", params={"location": "store_front"})

    assert response.status_code == 200
    levels = {level["product_id"]: level for level in response.json()}
    assert other_location not in levels
    assert levels[with_expired] | {"product_id": None} == {
        "product_id": None,
        "location": "store_front",
        "on_hand": 15,
        "sellable": 10,
        "reserved": 0,
        "available": 10,
        "next_expiration": (TODAY + timedelta(days=30)).isoformat(),
    }
    assert (levels[reserved]["sellable"], levels[reserved]["reserved"]) == (12, 3)
    assert levels[reserved]["available"] == 9
    assert levels[reserved]["next_expiration"] == (TODAY + timedelta(days=5)).isoformat()
    for product_id, level in levels.items():
        summary = await stock_of(client, product_id)
        [location] = [loc for loc in summary["locations"] if loc["location"] == "store_front"]
        assert level["available"] == location["available"]
