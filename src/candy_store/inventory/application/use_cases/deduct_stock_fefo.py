from collections.abc import Sequence
from dataclasses import dataclass

from candy_store.inventory.application.dto import (
    DeductionResult,
    LineAllocations,
    StockLine,
    normalize_lines,
)
from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryUnitOfWork,
)
from candy_store.inventory.application.use_cases._lines import load_items_covering
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


@dataclass(frozen=True, slots=True)
class DeductStockFefoCommand:
    reference: StockReference
    location: StockLocation
    lines: Sequence[StockLine]


class DeductStockFefoUseCase:
    """Venta POS: descuenta todas las líneas por FEFO en una transacción (todo o nada).

    Idempotente por referencia: repetir la misma venta devuelve el resultado
    original sin volver a descontar.
    """

    def __init__(self, uow: InventoryUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: DeductStockFefoCommand) -> DeductionResult:
        lines = normalize_lines(command.lines)
        today = self._clock.today()
        async with self._uow as uow:
            stored = await uow.operations.get(command.reference, InventoryOperation.SALE_DEDUCTION)
            if stored is not None:
                return DeductionResult.from_json(stored)

            items = await load_items_covering(uow, command.location, lines, today)
            result = DeductionResult(
                tuple(
                    LineAllocations(
                        product_id=item.product_id,
                        allocations=item.deduct(line.quantity, today, reference=command.reference),
                    )
                    for item, line in zip(items, lines, strict=True)
                )
            )
            await uow.operations.add(
                command.reference, InventoryOperation.SALE_DEDUCTION, result.to_json()
            )
            await uow.commit()
        return result
