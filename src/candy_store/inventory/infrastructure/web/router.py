"""Adaptador de entrada HTTP de inventario: traduce requests a comandos de casos de uso."""

from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.application.use_cases.confirm_reservation import (
    ConfirmReservationUseCase,
)
from candy_store.inventory.application.use_cases.deduct_stock_fefo import (
    DeductStockFefoCommand,
    DeductStockFefoUseCase,
)
from candy_store.inventory.application.use_cases.get_stock_summary import (
    GetStockSummaryUseCase,
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
from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseReservationUseCase,
)
from candy_store.inventory.application.use_cases.reserve_stock import (
    ReserveStockCommand,
    ReserveStockUseCase,
)
from candy_store.inventory.domain.errors import (
    BatchNotFoundError,
    DuplicateBatchError,
    DuplicateReservationError,
    InsufficientStockError,
    ReservationNotFoundError,
)
from candy_store.inventory.domain.value_objects import AdjustmentReason, BatchId, ReservationId
from candy_store.inventory.infrastructure.persistence.unit_of_work import (
    SqlAlchemyInventoryUnitOfWork,
)
from candy_store.inventory.infrastructure.web.schemas import (
    AllocationsResponse,
    AuditCountRequest,
    AuditCountResponse,
    ReceiveBatchRequest,
    ReceiveBatchResponse,
    RegisterLossRequest,
    ReservationResponse,
    StockMovementRequest,
    StockSummaryResponse,
)
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.web.dependencies import (
    ClockDep,
    SessionFactoryDep,
    SettingsDep,
)
from candy_store.shared.infrastructure.web.errors import ErrorStatusMap

ERROR_STATUS: ErrorStatusMap = {
    InsufficientStockError: status.HTTP_409_CONFLICT,
    DuplicateBatchError: status.HTTP_409_CONFLICT,
    DuplicateReservationError: status.HTTP_409_CONFLICT,
    BatchNotFoundError: status.HTTP_404_NOT_FOUND,
    ReservationNotFoundError: status.HTTP_404_NOT_FOUND,
}


def _inventory_uow(session_factory: SessionFactoryDep) -> InventoryUnitOfWork:
    return SqlAlchemyInventoryUnitOfWork(session_factory)


UowDep = Annotated[InventoryUnitOfWork, Depends(_inventory_uow)]

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


@router.post("/deductions")
async def deduct_stock(
    body: StockMovementRequest, uow: UowDep, clock: ClockDep
) -> AllocationsResponse:
    allocations = await DeductStockFefoUseCase(uow, clock).execute(
        DeductStockFefoCommand(
            product_id=ProductId(body.product_id), location=body.location, quantity=body.quantity
        )
    )
    return AllocationsResponse.from_domain(allocations)


@router.post("/reservations", status_code=status.HTTP_201_CREATED)
async def reserve_stock(
    body: StockMovementRequest, uow: UowDep, clock: ClockDep, settings: SettingsDep
) -> ReservationResponse:
    ttl = timedelta(minutes=settings.reservation_ttl_minutes)
    receipt = await ReserveStockUseCase(uow, clock, ttl).execute(
        ReserveStockCommand(
            product_id=ProductId(body.product_id), location=body.location, quantity=body.quantity
        )
    )
    return ReservationResponse(
        reservation_id=receipt.reservation_id.value, expires_at=receipt.expires_at
    )


@router.post("/reservations/{reservation_id}/confirmation")
async def confirm_reservation(
    reservation_id: UUID, uow: UowDep, clock: ClockDep
) -> AllocationsResponse:
    allocations = await ConfirmReservationUseCase(uow, clock).execute(ReservationId(reservation_id))
    return AllocationsResponse.from_domain(allocations)


@router.delete("/reservations/{reservation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release_reservation(reservation_id: UUID, uow: UowDep) -> None:
    await ReleaseReservationUseCase(uow).execute(ReservationId(reservation_id))


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
