from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from types import TracebackType
from typing import Self

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from candy_store.shared.application.unit_of_work import ConcurrencyError, UnitOfWork
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.infrastructure.outbox import OutboxMessage

_UNIQUE_VIOLATION = "23505"


class SqlAlchemyUnitOfWork(UnitOfWork):
    """Una sesión (y transacción) por bloque `async with`.

    Al confirmar: sincroniza los agregados tocados con sus filas, escribe sus
    eventos en el outbox y hace commit, todo en la misma transacción.
    """

    context: str

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("La UnitOfWork debe usarse dentro de `async with`")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self._start_repositories(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc, tb)
        finally:
            await self.session.close()
            self._session = None

    async def commit(self) -> None:
        events = [event for agg in self._tracked_aggregates() for event in agg.pull_events()]
        try:
            self._persist_tracked_aggregates()
            self.session.add_all(OutboxMessage.from_event(self.context, e) for e in events)
            await self.session.commit()
        except StaleDataError as exc:
            # Bloqueo optimista: la versión leída ya no es la vigente.
            await self.session.rollback()
            raise ConcurrencyError(str(exc)) from exc
        except IntegrityError as exc:
            await self.session.rollback()
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                # Dos transacciones crearon el mismo agregado a la vez.
                raise ConcurrencyError(str(exc.orig)) from exc
            raise

    async def rollback(self) -> None:
        await self.session.rollback()

    @abstractmethod
    def _start_repositories(self, session: AsyncSession) -> None: ...

    @abstractmethod
    def _tracked_aggregates(self) -> Sequence[AggregateRoot]: ...

    @abstractmethod
    def _persist_tracked_aggregates(self) -> None:
        """Vuelca el estado de los agregados a la sesión (sin commit)."""
