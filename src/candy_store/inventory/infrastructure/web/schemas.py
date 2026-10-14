"""Contratos HTTP de inventario (Pydantic). Validan forma; las reglas de negocio
siguen en el dominio."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt, StringConstraints

from candy_store.inventory.application.use_cases.get_stock_summary import (
    BatchView,
    LocationStock,
    StockSummary,
)
from candy_store.inventory.domain.value_objects import BatchAllocation, StockLocation

LotNumberStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReceiveBatchRequest(_Request):
    product_id: UUID
    location: StockLocation
    lot_number: LotNumberStr
    expiration: date
    quantity: PositiveInt


class ReceiveBatchResponse(BaseModel):
    batch_id: UUID


class StockMovementRequest(_Request):
    product_id: UUID
    location: StockLocation
    quantity: PositiveInt


class BatchAllocationOut(BaseModel):
    batch_id: UUID
    lot_number: str
    expiration: date
    quantity: int

    @classmethod
    def from_domain(cls, allocation: BatchAllocation) -> BatchAllocationOut:
        return cls(
            batch_id=allocation.batch_id.value,
            lot_number=allocation.lot_number.value,
            expiration=allocation.expiration.value,
            quantity=allocation.quantity,
        )


class AllocationsResponse(BaseModel):
    allocations: list[BatchAllocationOut]

    @classmethod
    def from_domain(cls, allocations: tuple[BatchAllocation, ...]) -> AllocationsResponse:
        return cls(allocations=[BatchAllocationOut.from_domain(a) for a in allocations])


class ReservationResponse(BaseModel):
    reservation_id: UUID
    expires_at: datetime


class RegisterLossRequest(_Request):
    product_id: UUID
    location: StockLocation
    batch_id: UUID
    quantity: PositiveInt
    # Las correcciones por auditoría van por /audit-counts.
    reason: Literal["shrinkage", "breakage", "expired"]


class AuditCountRequest(_Request):
    product_id: UUID
    location: StockLocation
    batch_id: UUID
    counted_quantity: NonNegativeInt


class AuditCountResponse(BaseModel):
    delta: int


class BatchOut(BaseModel):
    batch_id: UUID
    lot_number: str
    expiration: date
    quantity_on_hand: int
    expired: bool

    @classmethod
    def from_view(cls, view: BatchView) -> BatchOut:
        return cls(
            batch_id=view.batch_id.value,
            lot_number=view.lot_number,
            expiration=view.expiration,
            quantity_on_hand=view.quantity_on_hand,
            expired=view.expired,
        )


class LocationStockOut(BaseModel):
    location: StockLocation
    on_hand: int
    sellable: int
    reserved: int
    available: int
    batches: list[BatchOut]

    @classmethod
    def from_view(cls, view: LocationStock) -> LocationStockOut:
        return cls(
            location=view.location,
            on_hand=view.on_hand,
            sellable=view.sellable,
            reserved=view.reserved,
            available=view.available,
            batches=[BatchOut.from_view(b) for b in view.batches],
        )


class StockSummaryResponse(BaseModel):
    product_id: UUID
    total_available: int
    locations: list[LocationStockOut]

    @classmethod
    def from_view(cls, summary: StockSummary) -> StockSummaryResponse:
        return cls(
            product_id=summary.product_id.value,
            total_available=summary.total_available,
            locations=[LocationStockOut.from_view(loc) for loc in summary.locations],
        )
