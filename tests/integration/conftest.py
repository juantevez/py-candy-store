import asyncio
import hashlib
import hmac
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.main import configure_services, create_app
from candy_store.orm import metadata
from candy_store.shared.infrastructure.database import create_session_factory
from candy_store.shared.infrastructure.event_bus import EventBus
from candy_store.shared.infrastructure.outbox import OutboxRelay
from candy_store.shared.infrastructure.settings import Settings
from tests.fakes import FakePaymentGateway, FixedClock
from tests.unit.inventory.conftest import NOW, TODAY

TEST_DATABASE_URL = os.environ.get(
    "CANDY_TEST_DATABASE_URL",
    "postgresql+asyncpg://candy:candy@localhost:5433/candy_store_test",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _can_connect() -> bool:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except OSError:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Aplica las migraciones reales desde cero: también valida que funcionen."""
    if not asyncio.run(_can_connect()):
        pytest.skip(f"Postgres no disponible en {TEST_DATABASE_URL} (docker compose up -d)")
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    # NullPool: pytest-asyncio usa un event loop por test y asyncpg no comparte
    # conexiones entre loops.
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    # DELETE es mucho más rápido que TRUNCATE para tablas casi vacías.
    async with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            await conn.execute(table.delete())
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


# --- Tienda completa por HTTP (catálogo + inventario + ventas) -------------------

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

    async def stock(self, product_id: str) -> dict[str, dict[str, Any]]:
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
