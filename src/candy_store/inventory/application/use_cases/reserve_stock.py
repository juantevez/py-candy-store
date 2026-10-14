from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from candy_store.inventory.application.dto import ReservationReceipt, StockLine, normalize_lines
from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryUnitOfWork,
)
from candy_store.inventory.application.use_cases._lines import load_items_covering
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


@dataclass(frozen=True, slots=True)
class ReserveStockCommand:
    reference: StockReference
    location: StockLocation
    lines: Sequence[StockLine]


class ReserveStockUseCase:
    """Checkout online: aparta todas las líneas de la orden (todo o nada) hasta que
    se confirme el pago o venza el TTL. Idempotente por referencia."""

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock, ttl: timedelta) -> None:
        self._uow = uow
        self._clock = clock
        self._ttl = ttl

    @retry_on_concurrency()
    async def execute(self, command: ReserveStockCommand) -> ReservationReceipt:
        lines = normalize_lines(command.lines)
        today = self._clock.today()
        async with self._uow as uow:
            stored = await uow.operations.get(command.reference, InventoryOperation.RESERVATION)
            if stored is not None:
                return ReservationReceipt.from_json(stored)

            receipt = ReservationReceipt(expires_at=self._clock.now() + self._ttl)
            items = await load_items_covering(uow, command.location, lines, today)
            for item, line in zip(items, lines, strict=True):
                item.reserve(
                    reference=command.reference,
                    quantity=line.quantity,
                    expires_at=receipt.expires_at,
                    today=today,
                )
            await uow.operations.add(
                command.reference, InventoryOperation.RESERVATION, receipt.to_json()
            )
            await uow.commit()
        return receipt
