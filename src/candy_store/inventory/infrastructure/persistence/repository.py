from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.inventory.application.ports.repository import InventoryRepository
from candy_store.inventory.domain.model import InventoryItem, StockBatch, StockReservation
from candy_store.inventory.domain.value_objects import (
    BatchId,
    ExpirationDate,
    LotNumber,
    ReservationId,
    StockLocation,
)
from candy_store.inventory.infrastructure.persistence.models import (
    InventoryItemRow,
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

    async def get_by_reservation(self, reservation_id: ReservationId) -> InventoryItem | None:
        row = (
            await self._session.execute(
                select(StockReservationRow.product_id, StockReservationRow.location).where(
                    StockReservationRow.id == reservation_id.value
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return await self.get(ProductId(row.product_id), StockLocation(row.location))

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
                reservation_id=ReservationId(r.id), quantity=r.quantity, expires_at=r.expires_at
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
    current = {r.reservation_id.value: r for r in item.reservations}
    kept = [r for r in row.reservations if r.id in current]
    kept_ids = {r.id for r in kept}
    row.reservations = kept + [
        StockReservationRow(id=rid, quantity=r.quantity, expires_at=r.expires_at)
        for rid, r in current.items()
        if rid not in kept_ids
    ]
