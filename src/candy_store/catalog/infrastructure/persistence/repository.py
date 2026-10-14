from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.catalog.application.ports.repository import CatalogRepository
from candy_store.catalog.domain.model import Product
from candy_store.catalog.domain.value_objects import Sku
from candy_store.catalog.infrastructure.persistence.models import CatalogProductRow
from candy_store.shared.domain.value_objects import Money, ProductId


class SqlAlchemyCatalogRepository(CatalogRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._seen: dict[ProductId, tuple[Product, CatalogProductRow | None]] = {}

    async def get(self, product_id: ProductId) -> Product | None:
        if product_id in self._seen:
            return self._seen[product_id][0]
        row = await self._session.get(CatalogProductRow, product_id.value)
        return self._track(row) if row else None

    async def get_by_sku(self, sku: Sku) -> Product | None:
        row = await self._session.scalar(
            select(CatalogProductRow).where(CatalogProductRow.sku == sku.value)
        )
        return self._track(row) if row else None

    async def get_many(self, product_ids: Iterable[ProductId]) -> list[Product]:
        ids = [pid.value for pid in product_ids]
        rows = await self._session.scalars(
            select(CatalogProductRow).where(CatalogProductRow.id.in_(ids))
        )
        return [self._track(row) for row in rows]

    async def list_all(self, *, limit: int, offset: int) -> list[Product]:
        rows = await self._session.scalars(
            select(CatalogProductRow).order_by(CatalogProductRow.sku).limit(limit).offset(offset)
        )
        return [self._track(row) for row in rows]

    async def add(self, product: Product) -> None:
        self._seen[product.product_id] = (product, None)

    @property
    def seen(self) -> list[Product]:
        return [product for product, _ in self._seen.values()]

    def persist_seen(self) -> None:
        for product_id, (product, row) in self._seen.items():
            if row is None:
                row = CatalogProductRow(id=product_id.value, version=0)
                self._session.add(row)
                self._seen[product_id] = (product, row)
            row.sku = product.sku.value
            row.name = product.name
            row.price_amount = product.price.amount
            row.price_currency = product.price.currency
            row.active = product.active
            row.version = product.version + 1
            product.version = row.version

    def _track(self, row: CatalogProductRow) -> Product:
        product_id = ProductId(row.id)
        if product_id not in self._seen:
            product = Product(
                product_id=product_id,
                sku=Sku(row.sku),
                name=row.name,
                price=Money(row.price_amount, row.price_currency),
                active=row.active,
                version=row.version,
            )
            self._seen[product_id] = (product, row)
        return self._seen[product_id][0]
