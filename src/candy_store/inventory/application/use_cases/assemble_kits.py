from dataclasses import dataclass
from uuid import uuid4

from candy_store.inventory.application.dto import AssemblyResult, StockLine, normalize_lines
from candy_store.inventory.application.ports.kit_recipes import KitRecipeProvider
from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryUnitOfWork,
)
from candy_store.inventory.application.use_cases._lines import load_items_covering
from candy_store.inventory.domain.errors import InvalidQuantityError, KitRecipeNotFoundError
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ComponentUsage,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class AssembleKitsCommand:
    # Lo genera el back-office: reenviar el mismo armado no consume componentes dos veces.
    assembly_id: StockReference
    kit_product_id: ProductId
    location: StockLocation
    quantity: int
    lot_number: str | None = None  # si no se informa, se genera uno


class AssembleKitsUseCase:
    """Arma kits pre-armados en una ubicación: consume los componentes por FEFO y
    crea un lote del kit, todo en una transacción (todo o nada).

    Usa la receta vigente del catálogo; si después cambia, los kits ya armados
    conservan la trazabilidad de los componentes con que se armaron.
    """

    def __init__(self, uow: InventoryUnitOfWork, recipes: KitRecipeProvider, clock: Clock) -> None:
        self._uow = uow
        self._recipes = recipes
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: AssembleKitsCommand) -> AssemblyResult:
        if command.quantity <= 0:
            raise InvalidQuantityError(f"La cantidad de kits debe ser positiva: {command.quantity}")
        recipe = await self._recipes.get(command.kit_product_id)
        if recipe is None:
            raise KitRecipeNotFoundError(f"El producto {command.kit_product_id} no es un kit")

        lines = normalize_lines(
            StockLine(c.product_id, c.quantity * command.quantity) for c in recipe.components
        )
        today = self._clock.today()
        async with self._uow as uow:
            operation = InventoryOperation.KIT_ASSEMBLY
            stored = await uow.operations.get(command.assembly_id, operation)
            if stored is not None:
                return AssemblyResult.from_json(stored)

            components = await load_items_covering(uow, command.location, lines, today)
            usages = tuple(
                ComponentUsage(
                    product_id=item.product_id,
                    allocations=item.consume_for_kits(
                        line.quantity, today, reference=command.assembly_id
                    ),
                )
                for item, line in zip(components, lines, strict=True)
            )

            kit = await uow.items.get(command.kit_product_id, command.location)
            if kit is None:
                kit = InventoryItem(product_id=command.kit_product_id, location=command.location)
                await uow.items.add(kit)
            batch_id = BatchId.new()
            lot_number = LotNumber(command.lot_number or _generated_lot(today.strftime("%Y%m%d")))
            expiration = kit.add_assembled_kits(
                batch_id=batch_id,
                lot_number=lot_number,
                quantity=command.quantity,
                components=usages,
                assembled_at=self._clock.now(),
                today=today,
                reference=command.assembly_id,
            )

            result = AssemblyResult(
                kit_product_id=command.kit_product_id,
                batch_id=batch_id,
                lot_number=lot_number,
                expiration=expiration,
                quantity=command.quantity,
                components=usages,
            )
            await uow.operations.add(command.assembly_id, operation, result.to_json())
            await uow.commit()
        return result


def _generated_lot(day: str) -> str:
    return f"KIT-{day}-{uuid4().hex[:6].upper()}"
