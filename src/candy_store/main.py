"""Composition root: arma la aplicación conectando adaptadores con casos de uso.

Ejecutar: uv run uvicorn candy_store.main:create_app --factory --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.inventory.infrastructure.web import router as inventory_web
from candy_store.inventory.public import InventoryFacade
from candy_store.sales.infrastructure.inventory.stock_gateway import InProcessStockGateway
from candy_store.shared.infrastructure.background import PeriodicTask
from candy_store.shared.infrastructure.clock import SystemClock
from candy_store.shared.infrastructure.database import create_engine, create_session_factory
from candy_store.shared.infrastructure.event_bus import EventBus
from candy_store.shared.infrastructure.outbox import OutboxRelay
from candy_store.shared.infrastructure.settings import Settings, get_settings
from candy_store.shared.infrastructure.web import health
from candy_store.shared.infrastructure.web.errors import install_error_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url, echo=settings.database_echo)
        session_factory = create_session_factory(engine)
        clock = SystemClock()
        event_bus = EventBus()  # Los contextos suscriben aquí sus handlers de integración.

        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.clock = clock
        app.state.event_bus = event_bus

        # Comunicación entre contextos: sales usa inventory en proceso a través
        # de su API pública, envuelta en el StockGateway propio de sales.
        inventory_facade = InventoryFacade(
            uow_factory=lambda: SqlAlchemyInventoryUnitOfWork(session_factory),
            clock=clock,
            reservation_ttl=timedelta(minutes=settings.reservation_ttl_minutes),
        )
        app.state.stock_gateway = InProcessStockGateway(inventory_facade)

        tasks = [
            PeriodicTask(
                "outbox-relay",
                OutboxRelay(
                    session_factory, event_bus, batch_size=settings.outbox_batch_size
                ).publish_pending,
                settings.outbox_poll_interval_seconds,
            ),
            PeriodicTask(
                "release-expired-reservations",
                ReleaseExpiredReservationsUseCase(
                    SqlAlchemyInventoryUnitOfWork(session_factory), clock
                ).execute,
                settings.reservation_sweep_interval_seconds,
            ),
        ]
        if settings.background_tasks_enabled:
            for task in tasks:
                task.start()
        try:
            yield
        finally:
            for task in tasks:
                await task.stop()
            await engine.dispose()

    app = FastAPI(title="Candy Store", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app, {**inventory_web.ERROR_STATUS})
    app.include_router(health.router)
    app.include_router(inventory_web.router)
    return app
