"""Lo que sales necesita del stock, expresado en el lenguaje de ventas.

Sales no conoce lotes, ubicaciones ni FEFO: eso es asunto de inventory. El
adaptador concreto vive en sales/infrastructure/inventory.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from candy_store.sales.domain.value_objects import OrderId
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class ItemQuantity:
    product_id: ProductId
    quantity: int


@dataclass(frozen=True, slots=True)
class UnavailableItem:
    product_id: ProductId
    requested: int
    available: int


class OutOfStockError(Exception):
    """No hay stock para cubrir todos los ítems. No se movió nada."""

    def __init__(self, items: tuple[UnavailableItem, ...]) -> None:
        super().__init__(f"Sin stock suficiente para {len(items)} producto(s)")
        self.items = items


class StockHoldLostError(Exception):
    """El stock apartado para la orden ya no existe (venció o se liberó). Si el
    pago se aprobó igual, hay que reintentar con stock libre o devolver el dinero."""


class StockGateway(ABC):
    """Todas las operaciones son idempotentes por `order_id`: reintentar es seguro."""

    @abstractmethod
    async def take_for_counter_sale(self, order_id: OrderId, items: Sequence[ItemQuantity]) -> None:
        """Venta de mostrador: descuenta el stock del salón en el acto."""

    @abstractmethod
    async def hold_for_online_order(
        self, order_id: OrderId, items: Sequence[ItemQuantity]
    ) -> datetime:
        """Checkout online: aparta stock y devuelve hasta cuándo se mantiene."""

    @abstractmethod
    async def commit_online_order(self, order_id: OrderId) -> None:
        """Pago aprobado: el stock apartado sale definitivamente.

        Lanza StockHoldLostError si ya no hay stock apartado para la orden.
        """

    @abstractmethod
    async def cancel_online_order(self, order_id: OrderId) -> None:
        """Pago rechazado o checkout abandonado: devuelve el stock apartado.
        No falla si el stock ya había sido liberado."""
