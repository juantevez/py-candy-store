from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self


class UnitOfWork(ABC):
    """Frontera transaccional de un caso de uso.

    Cada contexto la extiende exponiendo sus repositorios. Si el bloque termina
    sin `commit()` explícito (o con excepción), se descartan los cambios.
    La implementación concreta persiste los eventos de dominio de los agregados
    tocados en la misma transacción (transactional outbox).
    """

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...
