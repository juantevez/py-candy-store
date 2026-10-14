from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.infrastructure.persistence.repository import SqlAlchemyOrderRepository
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


class SqlAlchemySalesUnitOfWork(SqlAlchemyUnitOfWork, SalesUnitOfWork):
    context = "sales"
    _repo: SqlAlchemyOrderRepository

    def _start_repositories(self, session: AsyncSession) -> None:
        self.orders = self._repo = SqlAlchemyOrderRepository(session)

    def _tracked_aggregates(self) -> Sequence[AggregateRoot]:
        return self._repo.seen

    def _persist_tracked_aggregates(self) -> None:
        self._repo.persist_seen()
