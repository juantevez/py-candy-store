from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryRepository,
    OperationLog,
)
from candy_store.inventory.domain.model import InventoryItem, StockBatch, StockReservation
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.inventory.infrastructure.persistence.models import (
    InventoryItemRow,
    ProcessedOperationRow,
    StockBatchRow,
    StockReservationRow,
)
from candy_store.shared.domain.value_objects import ProductId

type _Key = tuple[ProductId, StockLocation]


class SqlAlchemyInventoryRepository(InventoryRepository):
    """Mapea InventoryItem <-> filas ORM y recuerda los agregados tocados en la sesión."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._seen: dict[_Key, tuple[InventoryItem, InventoryItemRow | None]] = {}

    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        key = (product_id, location)
        if key in self._seen:
            return self._seen[key][0]
        row = await self._session.get(InventoryItemRow, (product_id.value, location.value))
        if row is None:
            return None
        item = _to_domain(row)
        self._seen[key] = (item, row)
        return item

    async def list_by_reservation(self, reference: StockReference) -> list[InventoryItem]:
        rows = await self._session.execute(
            select(StockReservationRow.product_id, StockReservationRow.location).where(
                StockReservationRow.reference == reference.value
            )
        )
        items = [
            await self.get(ProductId(row.product_id), StockLocation(row.location)) for row in rows
        ]
        return [item for item in items if item is not None]

    async def add(self, item: InventoryItem) -> None:
        self._seen[(item.product_id, item.location)] = (item, None)

    async def keys_with_expired_reservations(
        self, now: datetime, limit: int
    ) -> list[tuple[ProductId, StockLocation]]:
        rows = await self._session.execute(
            select(StockReservationRow.product_id, StockReservationRow.location)
            .where(StockReservationRow.expires_at <= now)
            .distinct()
            .limit(limit)
        )
        return [(ProductId(r.product_id), StockLocation(r.location)) for r in rows]

    @property
    def seen(self) -> list[InventoryItem]:
        return [item for item, _ in self._seen.values()]

    def persist_seen(self) -> None:
        for key, (item, row) in self._seen.items():
            if row is None:
                row = InventoryItemRow(
                    product_id=item.product_id.value, location=item.location.value, version=0
                )
                self._session.add(row)
                self._seen[key] = (item, row)
            _sync_rows(item, row)
            row.version = item.version + 1
            item.version = row.version


def _to_domain(row: InventoryItemRow) -> InventoryItem:
    return InventoryItem(
        product_id=ProductId(row.product_id),
        location=StockLocation(row.location),
        batches=[
            StockBatch(
                batch_id=BatchId(b.id),
                lot_number=LotNumber(b.lot_number),
                expiration=ExpirationDate(b.expiration),
                quantity_on_hand=b.quantity_on_hand,
                received_at=b.received_at,
            )
            for b in row.batches
        ],
        reservations=[
            StockReservation(
                reference=StockReference(r.reference), quantity=r.quantity, expires_at=r.expires_at
            )
            for r in row.reservations
        ],
        version=row.version,
    )


def _sync_rows(item: InventoryItem, row: InventoryItemRow) -> None:
    batch_rows = {b.id: b for b in row.batches}
    for batch in item.batches:
        existing = batch_rows.get(batch.batch_id.value)
        if existing is None:
            row.batches.append(
                StockBatchRow(
                    id=batch.batch_id.value,
                    lot_number=batch.lot_number.value,
                    expiration=batch.expiration.value,
                    quantity_on_hand=batch.quantity_on_hand,
                    received_at=batch.received_at,
                )
            )
        else:
            existing.quantity_on_hand = batch.quantity_on_hand

    # Las reservas son inmutables: solo se agregan o se eliminan (delete-orphan).
    current = {r.reference.value: r for r in item.reservations}
    kept = [r for r in row.reservations if r.reference in current]
    kept_refs = {r.reference for r in kept}
    row.reservations = kept + [
        StockReservationRow(reference=ref, quantity=r.quantity, expires_at=r.expires_at)
        for ref, r in current.items()
        if ref not in kept_refs
    ]


class SqlAlchemyOperationLog(OperationLog):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, reference: StockReference, operation: InventoryOperation
    ) -> dict[str, Any] | None:
        row = await self._session.get(ProcessedOperationRow, (reference.value, operation.value))
        return row.result if row else None

    async def add(
        self, reference: StockReference, operation: InventoryOperation, result: dict[str, Any]
    ) -> None:
        # Si otra transacción registró la misma operación en paralelo, el PK duplicado
        # se traduce en ConcurrencyError y el reintento devuelve el resultado guardado.
        self._session.add(
            ProcessedOperationRow(
                reference=reference.value, operation=operation.value, result=result
            )
        )
