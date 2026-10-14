from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.infrastructure.persistence.repository import (
    SqlAlchemyPurchaseOrderRepository,
    SqlAlchemyRemittanceRepository,
    SqlAlchemySupplierRepository,
)
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyPurchasingUnitOfWork(SqlAlchemyUnitOfWork, PurchasingUnitOfWork):
    context = "purchasing"
    _suppliers: SqlAlchemySupplierRepository
    _orders: SqlAlchemyPurchaseOrderRepository
    _remittances: SqlAlchemyRemittanceRepository

    def _start_repositories(self, session: AsyncSession) -> None:
        self.suppliers = self._suppliers = SqlAlchemySupplierRepository(session)
        self.orders = self._orders = SqlAlchemyPurchaseOrderRepository(session)
        self.remittances = self._remittances = SqlAlchemyRemittanceRepository(session)

    def _tracked_aggregates(self) -> Sequence[AggregateRoot]:
        return [*self._suppliers.seen, *self._orders.seen, *self._remittances.seen]

    def _persist_tracked_aggregates(self) -> None:
        self._suppliers.persist_seen()
        self._orders.persist_seen()
        self._remittances.persist_seen()
