"""Composition root: arma la aplicación conectando adaptadores con casos de uso.

Ejecutar: uv run uvicorn candy_store.main:create_app --factory --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import State

from candy_store.catalog.infrastructure.persistence.unit_of_work import (
    SqlAlchemyCatalogUnitOfWork,
)
from candy_store.catalog.infrastructure.web import router as catalog_web
from candy_store.catalog.public import CatalogFacade
from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseExpiredReservationsUseCase,
)
from candy_store.inventory.infrastructure.catalog.kit_recipes import InProcessKitRecipes
from candy_store.inventory.infrastructure.internal.purchasing_events import (
    REMITTANCE_RECEIVED,
    remittance_received_handler,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.inventory.infrastructure.web import router as inventory_web
from candy_store.inventory.public import InventoryFacade
from candy_store.purchasing.infrastructure.catalog.product_catalog import (
    InProcessPurchasingCatalog,
)
from candy_store.purchasing.infrastructure.web import router as purchasing_web
from candy_store.sales.application.ports.payment_gateway import PaymentGateway
from candy_store.sales.infrastructure.catalog.product_catalog import InProcessProductCatalog
from candy_store.sales.infrastructure.external.mercadopago import (
    MercadoPagoConfig,
    MercadoPagoGateway,
)
from candy_store.sales.infrastructure.internal.stock_events import (
    STOCK_RESERVATION_RELEASED,
    stock_hold_expired_handler,
)
from candy_store.sales.infrastructure.inventory.stock_gateway import InProcessStockGateway
from candy_store.sales.infrastructure.persistence.unit_of_work import SqlAlchemySalesUnitOfWork
from candy_store.sales.infrastructure.web import router as sales_web
from candy_store.shared.application.clock import Clock
from candy_store.shared.infrastructure.background import PeriodicTask
from candy_store.shared.infrastructure.clock import SystemClock
from candy_store.shared.infrastructure.database import create_engine, create_session_factory
from candy_store.shared.infrastructure.event_bus import EventBus
from candy_store.shared.infrastructure.outbox import OutboxRelay
from candy_store.shared.infrastructure.settings import Settings, get_settings
from candy_store.shared.infrastructure.web import health
from candy_store.shared.infrastructure.web.errors import install_error_handlers


def configure_services(
    state: State,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    event_bus: EventBus,
    payment_gateway: PaymentGateway,
) -> None:
    """Conecta los contextos entre sí. Los tests lo reutilizan con dobles de los
    servicios externos (la pasarela de pago)."""
    state.settings = settings
    state.session_factory = session_factory
    state.clock = clock
    state.event_bus = event_bus

    # Los contextos se usan en proceso a través de sus APIs públicas, envueltas
    # en los puertos propios de quien las consume.
    inventory_facade = InventoryFacade(
        uow_factory=lambda: SqlAlchemyInventoryUnitOfWork(session_factory),
        clock=clock,
        reservation_ttl=timedelta(minutes=settings.reservation_ttl_minutes),
    )
    catalog_facade = CatalogFacade(lambda: SqlAlchemyCatalogUnitOfWork(session_factory))
    state.stock_gateway = InProcessStockGateway(inventory_facade)
    state.product_catalog = InProcessProductCatalog(catalog_facade)
    # Inventory lee las recetas de kits del catálogo para armarlos.
    state.kit_recipes = InProcessKitRecipes(catalog_facade)
    state.purchasing_catalog = InProcessPurchasingCatalog(catalog_facade)
    state.payment_gateway = payment_gateway

    # Integración asincrónica (eventos del outbox):
    # - cuando vence una reserva, sales cancela la orden;
    # - cuando compras acepta un remito, inventory ingresa los lotes.
    event_bus.subscribe(
        STOCK_RESERVATION_RELEASED,
        stock_hold_expired_handler(lambda: SqlAlchemySalesUnitOfWork(session_factory), clock),
    )
    event_bus.subscribe(
        REMITTANCE_RECEIVED,
        remittance_received_handler(lambda: SqlAlchemyInventoryUnitOfWork(session_factory)),
    )


def _mercadopago(settings: Settings, client: httpx.AsyncClient) -> MercadoPagoGateway:
    storefront = settings.storefront_url.rstrip("/")
    return MercadoPagoGateway(
        client,
        MercadoPagoConfig(
            access_token=settings.mercadopago_access_token,
            notification_url=f"{settings.public_base_url.rstrip('/')}/sales/webhooks/mercadopago",
            success_url=f"{storefront}/checkout/exito",
            failure_url=f"{storefront}/checkout/error",
            pending_url=f"{storefront}/checkout/pendiente",
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url, echo=settings.database_echo)
        session_factory = create_session_factory(engine)
        clock = SystemClock()
        event_bus = EventBus()
        http_client = httpx.AsyncClient(
            base_url=settings.mercadopago_api_url, timeout=settings.mercadopago_timeout_seconds
        )
        configure_services(
            app.state,
            settings=settings,
            session_factory=session_factory,
            clock=clock,
            event_bus=event_bus,
            payment_gateway=_mercadopago(settings, http_client),
        )

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
            await http_client.aclose()
            await engine.dispose()

    app = FastAPI(title="Candy Store", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )
    install_error_handlers(
        app,
        {
            **catalog_web.ERROR_STATUS,
            **inventory_web.ERROR_STATUS,
            **purchasing_web.ERROR_STATUS,
            **sales_web.ERROR_STATUS,
        },
    )
    app.include_router(health.router)
    app.include_router(catalog_web.router)
    app.include_router(inventory_web.router)
    app.include_router(purchasing_web.router)
    app.include_router(sales_web.router)
    return app
