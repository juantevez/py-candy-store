"""Suscriptores de inventory a eventos de compras (payload JSON del outbox)."""

import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.application.use_cases.receive_supplier_delivery import (
    DeliveryLine,
    ReceiveSupplierDeliveryCommand,
    ReceiveSupplierDeliveryUseCase,
)
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.event_bus import EventHandler

logger = logging.getLogger(__name__)

REMITTANCE_RECEIVED = "RemittanceReceived"


def remittance_received_handler(uow_factory: Callable[[], InventoryUnitOfWork]) -> EventHandler:
    async def handle(payload: dict[str, Any]) -> None:
        command = ReceiveSupplierDeliveryCommand(
            reference=StockReference(str(payload["receipt_id"])),
            location=StockLocation(payload["location"]),
            received_at=datetime.fromisoformat(payload["received_at"]),
            received_on=date.fromisoformat(payload["received_on"]),
            lines=[
                DeliveryLine(
                    product_id=ProductId.from_string(line["product_id"]),
                    lot_number=line["lot_number"],
                    expiration=date.fromisoformat(line["expiration"]),
                    quantity=int(line["quantity"]),
                )
                for line in payload["lines"]
            ],
        )
        batches = await ReceiveSupplierDeliveryUseCase(uow_factory()).execute(command)
        logger.info("Remito %s ingresado al stock (%s lotes)", command.reference, batches)

    return handle
