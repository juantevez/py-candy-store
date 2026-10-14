import asyncio
import functools
import random
from collections.abc import Awaitable, Callable

from candy_store.shared.application.unit_of_work import ConcurrencyError


def retry_on_concurrency[**P, R](
    attempts: int = 3, base_delay_seconds: float = 0.01
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Reintenta un caso de uso completo si perdió una carrera de bloqueo optimista.

    Es seguro porque un `ConcurrencyError` implica que la transacción no confirmó
    nada, y cada intento vuelve a cargar el agregado con su estado vigente.
    """

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except ConcurrencyError:
                    if attempt == attempts:
                        raise
                    # Jitter para que los competidores no vuelvan a chocar en el mismo instante.
                    await asyncio.sleep(base_delay_seconds * attempt * (1 + random.random()))
            raise AssertionError("inalcanzable")

        return wrapper

    return decorator
