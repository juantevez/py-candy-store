"""Contratos HTTP de inventario (Pydantic). Validan forma; las reglas de negocio
siguen en el dominio."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt, StringConstraints

from candy_store.inventory.application.dto import AssemblyResult
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


class AssembleKitsRequest(_Request):
    assembly_id: UUID
    kit_product_id: UUID
    location: StockLocation
    quantity: PositiveInt
    lot_number: LotNumberStr | None = None


class AllocationOut(BaseModel):
    batch_id: UUID
    lot_number: str
    expiration: date
    quantity: int

    @classmethod
    def of(cls, allocation: BatchAllocation) -> AllocationOut:
        return cls(
            batch_id=allocation.batch_id.value,
            lot_number=allocation.lot_number.value,
            expiration=allocation.expiration.value,
            quantity=allocation.quantity,
        )


class ComponentUsageOut(BaseModel):
    product_id: UUID
    quantity: int
    allocations: list[AllocationOut]


class AssemblyResponse(BaseModel):
    kit_product_id: UUID
    batch_id: UUID
    lot_number: str
    expiration: date
    quantity: int
    components: list[ComponentUsageOut]

    @classmethod
    def from_result(cls, result: AssemblyResult) -> AssemblyResponse:
        return cls(
            kit_product_id=result.kit_product_id.value,
            batch_id=result.batch_id.value,
            lot_number=result.lot_number.value,
            expiration=result.expiration.value,
            quantity=result.quantity,
            components=[
                ComponentUsageOut(
                    product_id=c.product_id.value,
                    quantity=c.quantity,
                    allocations=[AllocationOut.of(a) for a in c.allocations],
                )
                for c in result.components
            ],
        )
