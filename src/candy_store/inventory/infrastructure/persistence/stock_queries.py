from datetime import date

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candy_store.inventory.application.ports.stock_queries import StockLevel, StockQueries
from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.inventory.infrastructure.persistence.models import (
    InventoryItemRow,
    StockBatchRow,
    StockReservationRow,
)
from candy_store.shared.domain.value_objects import ProductId


class SqlAlchemyStockQueries(StockQueries):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def levels_at(self, location: StockLocation, today: date) -> list[StockLevel]:
        sellable_batch = and_(StockBatchRow.expiration >= today, StockBatchRow.quantity_on_hand > 0)
        batches = (
            select(
                StockBatchRow.product_id,
                func.sum(StockBatchRow.quantity_on_hand).label("on_hand"),
                func.coalesce(
                    func.sum(StockBatchRow.quantity_on_hand).filter(sellable_batch), 0
                ).label("sellable"),
                func.min(StockBatchRow.expiration).filter(sellable_batch).label("next_expiration"),
            )
            .where(StockBatchRow.location == location.value)
            .group_by(StockBatchRow.product_id)
            .subquery()
        )
        reservations = (
            select(
                StockReservationRow.product_id,
                func.sum(StockReservationRow.quantity).label("reserved"),
            )
            .where(StockReservationRow.location == location.value)
            .group_by(StockReservationRow.product_id)
            .subquery()
        )
        query = (
            select(
                InventoryItemRow.product_id,
                func.coalesce(batches.c.on_hand, 0),
                func.coalesce(batches.c.sellable, 0),
                func.coalesce(reservations.c.reserved, 0),
                batches.c.next_expiration,
            )
            .outerjoin(batches, batches.c.product_id == InventoryItemRow.product_id)
            .outerjoin(reservations, reservations.c.product_id == InventoryItemRow.product_id)
            .where(InventoryItemRow.location == location.value)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return [
            StockLevel(
                product_id=ProductId(product_id),
                location=location,
                on_hand=int(on_hand),
                sellable=int(sellable),
                reserved=int(reserved),
                next_expiration=next_expiration,
            )
            for product_id, on_hand, sellable, reserved, next_expiration in rows
        ]
