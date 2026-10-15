"""Anti-corruption layer: traduce el StockGateway de sales a la API pública de
inventory. Es el único lugar de sales que conoce a inventory."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta

from candy_store.inventory import public as inventory
from candy_store.sales.application.ports.stock import (
    ItemQuantity,
    OutOfStockError,
    StockGateway,
    StockHoldLostError,
    UnavailableItem,
)
from candy_store.sales.domain.value_objects import OrderId

# Política de abastecimiento por canal: el mostrador vende lo que está en el
# salón; las órdenes online se preparan desde el depósito.
COUNTER_LOCATION = inventory.Location.STORE_FRONT
ONLINE_LOCATION = inventory.Location.WAREHOUSE


class InProcessStockGateway(StockGateway):
    def __init__(self, facade: inventory.InventoryFacade) -> None:
        self._inventory = facade

    async def take_for_counter_sale(self, order_id: OrderId, items: Sequence[ItemQuantity]) -> None:
        with _translate_errors():
            await self._inventory.deduct(str(order_id), COUNTER_LOCATION, _lines(items))

    async def hold_for_online_order(
        self,
        order_id: OrderId,
        items: Sequence[ItemQuantity],
        hold_for: timedelta | None = None,
    ) -> datetime:
        with _translate_errors():
            return await self._inventory.reserve(
                str(order_id), ONLINE_LOCATION, _lines(items), ttl=hold_for
            )

    async def commit_online_order(self, order_id: OrderId) -> None:
        with _translate_errors():
            await self._inventory.confirm_reservation(str(order_id))

    async def cancel_online_order(self, order_id: OrderId) -> None:
        # Si ya no había stock apartado, el objetivo de cancelar está cumplido.
        with suppress(inventory.ReservationMissingError):
            await self._inventory.release_reservation(str(order_id))


def _lines(items: Sequence[ItemQuantity]) -> list[inventory.StockRequestLine]:
    return [inventory.StockRequestLine(item.product_id, item.quantity) for item in items]


@contextmanager
def _translate_errors() -> Iterator[None]:
    try:
        yield
    except inventory.OutOfStockError as exc:
        raise OutOfStockError(
            tuple(UnavailableItem(s.product_id, s.requested, s.available) for s in exc.short_lines)
        ) from exc
    except inventory.ReservationMissingError as exc:
        raise StockHoldLostError(str(exc)) from exc
