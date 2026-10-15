"""API pública del contexto de inventario para otros contextos del monolito.

Es el único módulo de inventory que otros contextos pueden importar (lo verifica
import-linter). Expone tipos propios y del shared kernel, nunca el dominio ni los
casos de uso: inventory puede cambiar por dentro sin romper a sus clientes.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from candy_store.inventory.application.dto import StockLine
from candy_store.inventory.application.ports.repository import InventoryUnitOfWork
from candy_store.inventory.application.use_cases.confirm_reservation import (
    ConfirmReservationUseCase,
)
from candy_store.inventory.application.use_cases.deduct_stock_fefo import (
    DeductStockFefoCommand,
    DeductStockFefoUseCase,
)
from candy_store.inventory.application.use_cases.release_reservations import (
    ReleaseReservationUseCase,
)
from candy_store.inventory.application.use_cases.reserve_stock import (
    ReserveStockCommand,
    ReserveStockUseCase,
)
from candy_store.inventory.domain.errors import (
    InvalidQuantityError,
    ReservationNotFoundError,
    StockShortageError,
)
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.shared.application.clock import Clock
from candy_store.shared.domain.value_objects import ProductId

__all__ = [
    "InvalidStockRequestError",
    "InventoryError",
    "InventoryFacade",
    "Location",
    "OutOfStockError",
    "ReservationMissingError",
    "ShortLine",
    "StockRequestLine",
]

Location = StockLocation


@dataclass(frozen=True, slots=True)
class StockRequestLine:
    product_id: ProductId
    quantity: int


@dataclass(frozen=True, slots=True)
class ShortLine:
    product_id: ProductId
    requested: int
    available: int


class InventoryError(Exception):
    """Base de los errores que inventory expone a otros contextos."""


class OutOfStockError(InventoryError):
    def __init__(self, short_lines: tuple[ShortLine, ...]) -> None:
        super().__init__(f"Sin stock suficiente para {len(short_lines)} producto(s)")
        self.short_lines = short_lines


class ReservationMissingError(InventoryError):
    """No hay reserva activa para la referencia: no existió, ya se confirmó, se
    liberó o venció y la liberó el proceso periódico."""


class InvalidStockRequestError(InventoryError):
    pass


class InventoryFacade:
    """Operaciones de stock por venta/orden. Todas son atómicas (todo o nada) e
    idempotentes por `reference`: reintentar con la misma referencia es seguro."""

    def __init__(
        self,
        uow_factory: Callable[[], InventoryUnitOfWork],
        clock: Clock,
        reservation_ttl: timedelta,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._reservation_ttl = reservation_ttl

    async def deduct(
        self, reference: str, location: Location, lines: Sequence[StockRequestLine]
    ) -> None:
        """Salida inmediata por FEFO (venta presencial)."""
        with _translate_errors():
            await DeductStockFefoUseCase(self._uow_factory(), self._clock).execute(
                DeductStockFefoCommand(
                    reference=_reference(reference),
                    location=location,
                    lines=_to_stock_lines(lines),
                )
            )

    async def reserve(
        self,
        reference: str,
        location: Location,
        lines: Sequence[StockRequestLine],
        ttl: timedelta | None = None,
    ) -> datetime:
        """Aparta stock para un checkout por `ttl` (o la duración por defecto).
        Devuelve hasta cuándo se mantiene la reserva."""
        with _translate_errors():
            receipt = await ReserveStockUseCase(
                self._uow_factory(), self._clock, ttl or self._reservation_ttl
            ).execute(
                ReserveStockCommand(
                    reference=_reference(reference),
                    location=location,
                    lines=_to_stock_lines(lines),
                )
            )
        return receipt.expires_at

    async def confirm_reservation(self, reference: str) -> None:
        """Convierte la reserva en salida de stock (pago aprobado)."""
        with _translate_errors():
            await ConfirmReservationUseCase(self._uow_factory(), self._clock).execute(
                _reference(reference)
            )

    async def release_reservation(self, reference: str) -> None:
        """Devuelve el stock reservado (pago rechazado o checkout abandonado)."""
        with _translate_errors():
            await ReleaseReservationUseCase(self._uow_factory()).execute(_reference(reference))


def _to_stock_lines(lines: Sequence[StockRequestLine]) -> list[StockLine]:
    return [StockLine(product_id=line.product_id, quantity=line.quantity) for line in lines]


def _reference(raw: str) -> StockReference:
    try:
        return StockReference(raw)
    except ValueError as exc:
        raise InvalidStockRequestError(str(exc)) from exc


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Traduce errores internos de inventory a los errores públicos."""
    try:
        yield
    except StockShortageError as exc:
        raise OutOfStockError(
            tuple(ShortLine(s.product_id, s.requested, s.available) for s in exc.shortages)
        ) from exc
    except ReservationNotFoundError as exc:
        raise ReservationMissingError(str(exc)) from exc
    except InvalidQuantityError as exc:
        raise InvalidStockRequestError(str(exc)) from exc
