"""Helpers compartidos por los casos de uso de ventas.

Las ventas coordinan tres transacciones independientes (la orden, el stock y la
pasarela). La orden se guarda primero y avanza de estado en pasos cortos; como
las operaciones de stock son idempotentes por id de orden, cualquier paso se
puede reintentar si el proceso se corta a mitad de camino.
"""

from collections.abc import Callable, Sequence

from candy_store.sales.application.ports.product_catalog import ProductCatalog
from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.ports.stock import ItemQuantity
from candy_store.sales.domain.errors import (
    EmptyOrderError,
    InvalidOrderError,
    OrderNotFoundError,
    ProductNotSellableError,
)
from candy_store.sales.domain.model import Order
from candy_store.sales.domain.value_objects import OrderId, OrderLine
from candy_store.shared.domain.value_objects import ProductId


async def price_lines(catalog: ProductCatalog, items: Sequence[ItemQuantity]) -> list[OrderLine]:
    """Arma las líneas con el precio y nombre vigentes del catálogo."""
    quantities: dict[ProductId, int] = {}
    for item in items:
        if item.quantity <= 0:
            raise InvalidOrderError(f"La cantidad debe ser positiva: {item.quantity}")
        quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
    if not quantities:
        raise EmptyOrderError("La orden debe tener al menos un producto")

    found = await catalog.find(quantities)
    not_sellable = [str(pid) for pid in quantities if pid not in found or not found[pid].sellable]
    if not_sellable:
        raise ProductNotSellableError(not_sellable)
    return [
        OrderLine(
            product_id=pid,
            description=found[pid].name,
            unit_price=found[pid].unit_price,
            quantity=qty,
        )
        for pid, qty in quantities.items()
    ]


def stock_items(order: Order) -> list[ItemQuantity]:
    return [ItemQuantity(line.product_id, line.quantity) for line in order.lines]


async def find_order(uow: SalesUnitOfWork, order_id: OrderId) -> Order | None:
    async with uow:
        return await uow.orders.get(order_id)


async def add_order(uow: SalesUnitOfWork, order: Order) -> None:
    async with uow:
        await uow.orders.add(order)
        await uow.commit()


async def update_order(
    uow: SalesUnitOfWork, order_id: OrderId, change: Callable[[Order], None]
) -> Order:
    async with uow:
        order = await uow.orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(f"No existe la orden {order_id}")
        change(order)
        await uow.commit()
    return order
