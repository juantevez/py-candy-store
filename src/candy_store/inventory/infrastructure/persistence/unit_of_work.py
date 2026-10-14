from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.infrastructure.persistence.repository import (
    SqlAlchemyInventoryRepository,
)
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyInventoryUnitOfWork(SqlAlchemyUnitOfWork, InventoryUnitOfWork):
    context = "inventory"
    _repo: SqlAlchemyInventoryRepository

    def _start_repositories(self, session: AsyncSession) -> None:
        self.items = self._repo = SqlAlchemyInventoryRepository(session)

    def _tracked_aggregates(self) -> Sequence[AggregateRoot]:
        return self._repo.seen

    def _persist_tracked_aggregates(self) -> None:
        self._repo.persist_seen()
