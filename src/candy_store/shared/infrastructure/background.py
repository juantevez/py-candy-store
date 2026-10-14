import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class PeriodicTask:
    """Ejecuta un job cada `interval_seconds` dentro del proceso de la app.

    Un fallo del job se registra y no detiene el ciclo. Suficiente para el
    monolito; si se escala a varias instancias, los jobs ya toleran correr en
    paralelo (SKIP LOCKED en el outbox, bloqueo optimista en las reservas).
    """

    def __init__(
        self, name: str, job: Callable[[], Awaitable[object]], interval_seconds: float
    ) -> None:
        self.name = name
        self._job = job
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=self.name)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._job()
            except Exception:
                logger.exception("Falló la tarea periódica %s", self.name)
            await asyncio.sleep(self._interval)
