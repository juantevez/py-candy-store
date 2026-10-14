from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.main import create_app
from candy_store.shared.infrastructure.settings import Settings
from tests.fakes import FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

pytestmark = pytest.mark.integration


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], clock: FixedClock
) -> AsyncIterator[AsyncClient]:
    # ASGITransport no ejecuta el lifespan: se inyecta el estado a mano, sin
    # tareas de fondo, para que los tests sean deterministas.
    settings = Settings(reservation_ttl_minutes=15)
    app = create_app(settings)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.clock = clock
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def receive(
    client: AsyncClient,
    product_id: str,
    *,
    quantity: int,
    expires_in_days: int,
    location: str = "store_front",
) -> str:
    response = await client.post(
        "/inventory/batches",
        json={
            "product_id": product_id,
            "location": location,
            "lot_number": f"lote-{expires_in_days}",
            "expiration": (TODAY + timedelta(days=expires_in_days)).isoformat(),
            "quantity": quantity,
        },
    )
    assert response.status_code == 201, response.text
    batch_id: str = response.json()["batch_id"]
    return batch_id


async def stock_of(client: AsyncClient, product_id: str) -> dict[str, Any]:
    response = await client.get(f"/inventory/products/{product_id}/stock")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def movement(product_id: str, quantity: int, location: str = "store_front") -> dict[str, Any]:
    return {"product_id": product_id, "location": location, "quantity": quantity}


async def test_pos_sale_deducts_by_fefo(client: AsyncClient) -> None:
    product_id = str(uuid4())
    later = await receive(client, product_id, quantity=10, expires_in_days=60)
    sooner = await receive(client, product_id, quantity=3, expires_in_days=5)

    response = await client.post("/inventory/deductions", json=movement(product_id, 5))

    assert response.status_code == 200
    assert response.json()["allocations"] == [
        {"batch_id": sooner, "lot_number": "LOTE-5", "expiration": "2026-10-19", "quantity": 3},
        {"batch_id": later, "lot_number": "LOTE-60", "expiration": "2026-12-13", "quantity": 2},
    ]
    summary = await stock_of(client, product_id)
    assert summary["total_available"] == 8
    [location] = summary["locations"]
    assert [b["batch_id"] for b in location["batches"]] == [later]


async def test_insufficient_stock_is_409_with_context(client: AsyncClient) -> None:
    product_id = str(uuid4())
    await receive(client, product_id, quantity=2, expires_in_days=10)

    response = await client.post("/inventory/deductions", json=movement(product_id, 5))

    assert response.status_code == 409
    assert response.json() == {
        "code": "insufficient_stock",
        "detail": "Stock insuficiente: se pidieron 5, disponibles 2",
        "requested": 5,
        "available": 2,
    }


async def test_online_checkout_reservation_flow(client: AsyncClient) -> None:
    product_id = str(uuid4())
    await receive(client, product_id, quantity=10, expires_in_days=30, location="warehouse")

    reserved = await client.post(
        "/inventory/reservations", json=movement(product_id, 4, "warehouse")
    )
    assert reserved.status_code == 201
    reservation_id = reserved.json()["reservation_id"]
    assert reserved.json()["expires_at"] == (NOW + timedelta(minutes=15)).isoformat().replace(
        "+00:00", "Z"
    )
    assert (await stock_of(client, product_id))["total_available"] == 6

    confirmed = await client.post(f"/inventory/reservations/{reservation_id}/confirmation")
    assert confirmed.status_code == 200
    assert sum(a["quantity"] for a in confirmed.json()["allocations"]) == 4

    [location] = (await stock_of(client, product_id))["locations"]
    assert (location["on_hand"], location["reserved"], location["available"]) == (6, 0, 6)

    again = await client.post(f"/inventory/reservations/{reservation_id}/confirmation")
    assert again.status_code == 404
    assert again.json()["code"] == "reservation_not_found"


async def test_release_reservation(client: AsyncClient) -> None:
    product_id = str(uuid4())
    await receive(client, product_id, quantity=5, expires_in_days=30)
    reserved = await client.post("/inventory/reservations", json=movement(product_id, 5))
    reservation_id = reserved.json()["reservation_id"]

    response = await client.delete(f"/inventory/reservations/{reservation_id}")

    assert response.status_code == 204
    assert (await stock_of(client, product_id))["total_available"] == 5


async def test_expired_reservations_are_swept(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], clock: FixedClock
) -> None:
    product_id = str(uuid4())
    await receive(client, product_id, quantity=5, expires_in_days=30)
    await client.post("/inventory/reservations", json=movement(product_id, 5))
    assert (await stock_of(client, product_id))["total_available"] == 0

    clock.current = NOW + timedelta(minutes=16)
    released = await ReleaseExpiredReservationsUseCase(
        SqlAlchemyInventoryUnitOfWork(session_factory), clock
    ).execute()

    assert released == 1
    assert (await stock_of(client, product_id))["total_available"] == 5


async def test_losses_and_audit_counts(client: AsyncClient) -> None:
    product_id = str(uuid4())
    batch_id = await receive(client, product_id, quantity=10, expires_in_days=30)
    target = {"product_id": product_id, "location": "store_front", "batch_id": batch_id}

    loss = await client.post(
        "/inventory/losses", json={**target, "quantity": 2, "reason": "breakage"}
    )
    audit = await client.post("/inventory/audit-counts", json={**target, "counted_quantity": 9})

    assert loss.status_code == 204
    assert audit.json() == {"delta": 1}
    assert (await stock_of(client, product_id))["total_available"] == 9


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
        "/inventory/batches",
        json={
            "product_id": str(uuid4()),
            "location": "warehouse",
            "lot_number": "X",
            "expiration": (TODAY - timedelta(days=1)).isoformat(),
            "quantity": 1,
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "expired_batch_received"


@pytest.mark.parametrize(
    "body",
    [
        {"location": "store_front", "quantity": 1},  # falta product_id
        {"product_id": "no-uuid", "location": "store_front", "quantity": 1},
        {"product_id": str(uuid4()), "location": "garage", "quantity": 1},
        {"product_id": str(uuid4()), "location": "store_front", "quantity": 0},
        {"product_id": str(uuid4()), "location": "store_front", "quantity": 1, "extra": True},
    ],
)
async def test_invalid_payloads_are_rejected(client: AsyncClient, body: dict[str, Any]) -> None:
    response = await client.post("/inventory/deductions", json=body)
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
