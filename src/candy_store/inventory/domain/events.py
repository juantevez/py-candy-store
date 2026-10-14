from dataclasses import dataclass
from datetime import datetime

from candy_store.inventory.domain.value_objects import (
    AdjustmentReason,
    BatchAllocation,
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, kw_only=True)
class StockBatchReceived(DomainEvent):
    product_id: ProductId
    location: StockLocation
    batch_id: BatchId
    lot_number: LotNumber
    expiration: ExpirationDate
    quantity: int


@dataclass(frozen=True, kw_only=True)
class StockDeducted(DomainEvent):
    product_id: ProductId
    location: StockLocation
    reference: StockReference
    allocations: tuple[BatchAllocation, ...]
    from_reservation: bool


@dataclass(frozen=True, kw_only=True)
class StockReserved(DomainEvent):
    product_id: ProductId
    location: StockLocation
    reference: StockReference
    quantity: int
    expires_at: datetime


@dataclass(frozen=True, kw_only=True)
class StockReservationReleased(DomainEvent):
    product_id: ProductId
    location: StockLocation
    reference: StockReference
    quantity: int
    expired: bool


@dataclass(frozen=True, kw_only=True)
class StockAdjusted(DomainEvent):
    product_id: ProductId
    location: StockLocation
    batch_id: BatchId
    delta: int
    reason: AdjustmentReason
