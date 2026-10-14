from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.catalog.application.ports.repository import CatalogUnitOfWork
from candy_store.catalog.infrastructure.persistence.repository import SqlAlchemyCatalogRepository
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemyCatalogUnitOfWork(SqlAlchemyUnitOfWork, CatalogUnitOfWork):
    context = "catalog"
    _repo: SqlAlchemyCatalogRepository

    def _start_repositories(self, session: AsyncSession) -> None:
        self.products = self._repo = SqlAlchemyCatalogRepository(session)

    def _tracked_aggregates(self) -> Sequence[AggregateRoot]:
        return self._repo.seen

    def _persist_tracked_aggregates(self) -> None:
        self._repo.persist_seen()
