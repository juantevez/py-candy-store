"""Adaptador de entrada HTTP de inventario (back-office): ingresos, ajustes y consultas.

Las salidas de stock por ventas no se exponen acá: sales las hace a través de
`candy_store.inventory.public`, siempre asociadas a una venta u orden.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from candy_store.inventory.application.ports.kit_recipes import KitRecipeProvider
from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.application.use_cases.assemble_kits import (
    AssembleKitsCommand,
    AssembleKitsUseCase,
)
from candy_store.inventory.application.use_cases.get_stock_summary import (
    GetStockSummaryUseCase,
)
from candy_store.inventory.application.use_cases.list_stock_levels import (
    ListStockLevelsUseCase,
)
from candy_store.inventory.application.use_cases.receive_stock_batch import (
    ReceiveStockBatchCommand,
    ReceiveStockBatchUseCase,
)
from candy_store.inventory.application.use_cases.register_loss_adjustment import (
    RegisterAuditCountCommand,
    RegisterAuditCountUseCase,
    RegisterLossCommand,
    RegisterLossUseCase,
)
from candy_store.inventory.domain.errors import (
    BatchNotFoundError,
    DuplicateBatchError,
    InsufficientStockError,
    KitRecipeNotFoundError,
    StockShortageError,
)
from candy_store.inventory.domain.value_objects import (
    AdjustmentReason,
    BatchId,
    StockLocation,
    StockReference,
)
from candy_store.inventory.infrastructure.persistence.stock_queries import (
    SqlAlchemyStockQueries,
)
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.inventory.infrastructure.web.schemas import (
    AssembleKitsRequest,
    AssemblyResponse,
    AuditCountRequest,
    AuditCountResponse,
    ReceiveBatchRequest,
    ReceiveBatchResponse,
    RegisterLossRequest,
    StockLevelResponse,
    StockSummaryResponse,
)
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.web.dependencies import ClockDep, SessionFactoryDep
from candy_store.shared.infrastructure.web.errors import ErrorStatusMap

ERROR_STATUS: ErrorStatusMap = {
    InsufficientStockError: status.HTTP_409_CONFLICT,
    DuplicateBatchError: status.HTTP_409_CONFLICT,
    StockShortageError: status.HTTP_409_CONFLICT,
    BatchNotFoundError: status.HTTP_404_NOT_FOUND,
    KitRecipeNotFoundError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _inventory_uow(session_factory: SessionFactoryDep) -> InventoryUnitOfWork:
    return SqlAlchemyInventoryUnitOfWork(session_factory)


def _kit_recipes(request: Request) -> KitRecipeProvider:
    recipes: KitRecipeProvider = request.app.state.kit_recipes
    return recipes


UowDep = Annotated[InventoryUnitOfWork, Depends(_inventory_uow)]
KitRecipesDep = Annotated[KitRecipeProvider, Depends(_kit_recipes)]

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.post("/batches", status_code=status.HTTP_201_CREATED)
async def receive_batch(
    body: ReceiveBatchRequest, uow: UowDep, clock: ClockDep
) -> ReceiveBatchResponse:
    batch_id = await ReceiveStockBatchUseCase(uow, clock).execute(
        ReceiveStockBatchCommand(
            product_id=ProductId(body.product_id),
            location=body.location,
            lot_number=body.lot_number,
            expiration=body.expiration,
            quantity=body.quantity,
        )
    )
    return ReceiveBatchResponse(batch_id=batch_id.value)


@router.post("/losses", status_code=status.HTTP_204_NO_CONTENT)
async def register_loss(body: RegisterLossRequest, uow: UowDep) -> None:
    await RegisterLossUseCase(uow).execute(
        RegisterLossCommand(
            product_id=ProductId(body.product_id),
            location=body.location,
            batch_id=BatchId(body.batch_id),
            quantity=body.quantity,
            reason=AdjustmentReason(body.reason),
        )
    )


@router.post("/audit-counts")
async def register_audit_count(body: AuditCountRequest, uow: UowDep) -> AuditCountResponse:
    delta = await RegisterAuditCountUseCase(uow).execute(
        RegisterAuditCountCommand(
            product_id=ProductId(body.product_id),
            location=body.location,
            batch_id=BatchId(body.batch_id),
            counted_quantity=body.counted_quantity,
        )
    )
    return AuditCountResponse(delta=delta)


@router.get("/products/{product_id}/stock")
async def get_stock(product_id: UUID, uow: UowDep, clock: ClockDep) -> StockSummaryResponse:
    summary = await GetStockSummaryUseCase(uow, clock).execute(ProductId(product_id))
    return StockSummaryResponse.from_view(summary)


@router.post("/kit-assemblies", status_code=status.HTTP_201_CREATED)
async def assemble_kits(
    body: AssembleKitsRequest, uow: UowDep, recipes: KitRecipesDep, clock: ClockDep
) -> AssemblyResponse:
    """Arma kits pre-armados: consume componentes por FEFO y crea un lote del kit."""
    result = await AssembleKitsUseCase(uow, recipes, clock).execute(
        AssembleKitsCommand(
            assembly_id=StockReference(str(body.assembly_id)),
            kit_product_id=ProductId(body.kit_product_id),
            location=body.location,
            quantity=body.quantity,
            lot_number=body.lot_number,
        )
    )
    return AssemblyResponse.from_result(result)


@router.get("/stock")
async def list_stock_levels(
    location: StockLocation, session_factory: SessionFactoryDep, clock: ClockDep
) -> list[StockLevelResponse]:
    """Stock de todos los productos en una ubicación (ej. `store_front` para el mostrador)."""
    levels = await ListStockLevelsUseCase(SqlAlchemyStockQueries(session_factory), clock).execute(
        location
    )
    return [StockLevelResponse.of(level) for level in levels]
