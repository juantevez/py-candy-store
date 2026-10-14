"""Suscriptores de sales a eventos de integración de otros contextos.

Reciben el payload JSON del outbox (no las clases de inventory), así sales no
depende del dominio de inventory: el contrato es el formato del evento.
"""

import logging
from collections.abc import Callable
from typing import Any

from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.use_cases.cancel_expired_order import (
    CancelExpiredOrderUseCase,
)
from candy_store.sales.domain.value_objects import OrderId
from candy_store.shared.application.clock import Clock
from candy_store.shared.infrastructure.event_bus import EventHandler

logger = logging.getLogger(__name__)

STOCK_RESERVATION_RELEASED = "StockReservationReleased"


def stock_hold_expired_handler(
    uow_factory: Callable[[], SalesUnitOfWork], clock: Clock
) -> EventHandler:
    async def handle(payload: dict[str, Any]) -> None:
        if not payload.get("expired"):
            return  # Liberaciones pedidas por sales mismo: no hay nada que hacer.
        try:
            order_id = OrderId.from_string(str(payload["reference"]))
        except (KeyError, ValueError):
            logger.warning("Reserva vencida con referencia ajena a ventas: %s", payload)
            return
        if await CancelExpiredOrderUseCase(uow_factory(), clock).execute(order_id):
            logger.info("Orden %s cancelada: venció la reserva de stock", order_id)

    return handle
