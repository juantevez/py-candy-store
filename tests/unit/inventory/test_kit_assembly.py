from datetime import timedelta

import pytest

from candy_store.inventory.application.dto import StockLine
from candy_store.inventory.application.use_cases.assemble_kits import (
    AssembleKitsCommand,
    AssembleKitsUseCase,
)
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.application.use_cases.reserve_stock import (
    ReserveStockCommand,
    ReserveStockUseCase,
)
from candy_store.inventory.domain.errors import (
    InvalidKitAssemblyError,
    InvalidQuantityError,
    KitRecipeNotFoundError,
    StockShortageError,
)
from candy_store.inventory.domain.events import KitsAssembled, StockConsumedForKits
from candy_store.inventory.domain.value_objects import (
    BatchAllocation,
    BatchId,
    ComponentUsage,
    ExpirationDate,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.shared.domain.value_objects import ProductId
from tests.fakes import FakeInventoryUnitOfWork, FakeKitRecipes, FixedClock
from tests.unit.inventory.conftest import NOW, TODAY, make_batch, make_item

LOCATION = StockLocation.WAREHOUSE
KIT = ProductId.new()
ALFAJOR = ProductId.new()
TURRON = ProductId.new()


@pytest.fixture
def uow() -> FakeInventoryUnitOfWork:
    return FakeInventoryUnitOfWork()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def recipes() -> FakeKitRecipes:
    recipes = FakeKitRecipes()
    recipes.define(KIT, (ALFAJOR, 2), (TURRON, 1))  # Caja Navidad: 2 alfajores + 1 turrón
    return recipes


@pytest.fixture
def assemble(
    uow: FakeInventoryUnitOfWork, recipes: FakeKitRecipes, clock: FixedClock
) -> AssembleKitsUseCase:
    return AssembleKitsUseCase(uow, recipes, clock)


async def receive(
    uow: FakeInventoryUnitOfWork,
    clock: FixedClock,
    product_id: ProductId,
    quantity: int,
    expires_in_days: int,
    lot: str,
) -> BatchId:
    return await ReceiveStockBatchUseCase(uow, clock).execute(
        ReceiveStockBatchCommand(
            product_id=product_id,
            location=LOCATION,
            lot_number=lot,
            expiration=TODAY + timedelta(days=expires_in_days),
            quantity=quantity,
        )
    )


def command(quantity: int, *, lot_number: str | None = None) -> AssembleKitsCommand:
    return AssembleKitsCommand(
        assembly_id=StockReference("ARMADO-1"),
        kit_product_id=KIT,
        location=LOCATION,
        quantity=quantity,
        lot_number=lot_number,
    )


async def on_hand(uow: FakeInventoryUnitOfWork, product_id: ProductId) -> int:
    item = await uow.items.get(product_id, LOCATION)
    return item.quantity_on_hand() if item else 0


async def test_assembly_consumes_components_by_fefo_and_creates_kit_batch(
    assemble: AssembleKitsUseCase, uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    alfajor_late = await receive(uow, clock, ALFAJOR, 20, expires_in_days=90, lot="ALF-B")
    alfajor_soon = await receive(uow, clock, ALFAJOR, 4, expires_in_days=40, lot="ALF-A")
    await receive(uow, clock, TURRON, 10, expires_in_days=200, lot="TUR-A")

    result = await assemble.execute(command(5, lot_number="NAV-2026"))

    assert result.quantity == 5
    assert result.lot_number == LotNumber("NAV-2026")
    # El kit vence con su primer componente: el lote de alfajores que vence a los 40 días.
    assert result.expiration == ExpirationDate(TODAY + timedelta(days=40))
    usage = {c.product_id: c for c in result.components}
    assert [(a.batch_id, a.quantity) for a in usage[ALFAJOR].allocations] == [
        (alfajor_soon, 4),
        (alfajor_late, 6),
    ]
    assert usage[TURRON].quantity == 5
    assert await on_hand(uow, ALFAJOR) == 14
    assert await on_hand(uow, TURRON) == 5
    kit = await uow.items.get(KIT, LOCATION)
    assert kit is not None
    [kit_batch] = kit.batches
    assert (kit_batch.batch_id, kit_batch.quantity_on_hand) == (result.batch_id, 5)

    consumed = [e for e in uow.published_events if isinstance(e, StockConsumedForKits)]
    assert {e.product_id for e in consumed} == {ALFAJOR, TURRON}
    [assembled] = [e for e in uow.published_events if isinstance(e, KitsAssembled)]
    assert assembled.components == result.components


async def test_lot_number_is_generated_when_not_given(
    assemble: AssembleKitsUseCase, uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    await receive(uow, clock, ALFAJOR, 10, expires_in_days=40, lot="A")
    await receive(uow, clock, TURRON, 10, expires_in_days=40, lot="T")

    result = await assemble.execute(command(1))

    assert result.lot_number.value.startswith("KIT-20261014-")


async def test_assembly_is_all_or_nothing(
    assemble: AssembleKitsUseCase, uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    await receive(uow, clock, ALFAJOR, 20, expires_in_days=40, lot="A")
    await receive(uow, clock, TURRON, 2, expires_in_days=40, lot="T")

    with pytest.raises(StockShortageError) as exc:
        await assemble.execute(command(5))

    [shortage] = exc.value.shortages
    assert (shortage.product_id, shortage.requested, shortage.available) == (TURRON, 5, 2)
    assert await on_hand(uow, ALFAJOR) == 20
    assert await uow.items.get(KIT, LOCATION) is None


async def test_assembly_is_idempotent(
    assemble: AssembleKitsUseCase, uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    await receive(uow, clock, ALFAJOR, 20, expires_in_days=40, lot="A")
    await receive(uow, clock, TURRON, 20, expires_in_days=40, lot="T")

    first = await assemble.execute(command(3))
    retry = await assemble.execute(command(3))

    assert retry == first
    assert await on_hand(uow, ALFAJOR) == 14
    assert await on_hand(uow, KIT) == 3


async def test_components_reserved_for_online_orders_are_not_used(
    assemble: AssembleKitsUseCase, uow: FakeInventoryUnitOfWork, clock: FixedClock
) -> None:
    await receive(uow, clock, ALFAJOR, 10, expires_in_days=40, lot="A")
    await receive(uow, clock, TURRON, 10, expires_in_days=40, lot="T")
    await ReserveStockUseCase(uow, clock, timedelta(minutes=15)).execute(
        ReserveStockCommand(
            reference=StockReference("ORD-1"),
            location=LOCATION,
            lines=[StockLine(ALFAJOR, 8)],
        )
    )

    with pytest.raises(StockShortageError):
        await assemble.execute(command(2))  # necesita 4 alfajores, quedan 2 libres


async def test_only_kits_can_be_assembled(
    assemble: AssembleKitsUseCase,
) -> None:
    with pytest.raises(KitRecipeNotFoundError):
        await assemble.execute(
            AssembleKitsCommand(
                assembly_id=StockReference("X"),
                kit_product_id=ALFAJOR,
                location=LOCATION,
                quantity=1,
            )
        )


async def test_quantity_must_be_positive(assemble: AssembleKitsUseCase) -> None:
    with pytest.raises(InvalidQuantityError):
        await assemble.execute(command(0))


def test_kit_batch_needs_components() -> None:
    kit = make_item()
    with pytest.raises(InvalidKitAssemblyError):
        kit.add_assembled_kits(
            batch_id=BatchId.new(),
            lot_number=LotNumber("K"),
            quantity=1,
            components=(),
            assembled_at=NOW,
            today=TODAY,
            reference=StockReference("A"),
        )


def test_kit_cannot_be_its_own_component() -> None:
    component = make_batch(quantity=5, expires_in_days=10)
    kit = make_item()
    allocation = BatchAllocation(component.batch_id, component.lot_number, component.expiration, 1)

    with pytest.raises(InvalidKitAssemblyError):
        kit.add_assembled_kits(
            batch_id=BatchId.new(),
            lot_number=LotNumber("K"),
            quantity=1,
            components=(ComponentUsage(product_id=kit.product_id, allocations=(allocation,)),),
            assembled_at=NOW,
            today=TODAY,
            reference=StockReference("A"),
        )
