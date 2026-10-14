from collections.abc import Sequence
from dataclasses import dataclass

from candy_store.sales.application.ports.product_catalog import ProductCatalog
from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.ports.stock import ItemQuantity, OutOfStockError, StockGateway
from candy_store.sales.application.use_cases._orders import (
    add_order,
    find_order,
    price_lines,
    stock_items,
    update_order,
)
from candy_store.sales.application.use_cases.get_order import OrderView
from candy_store.sales.domain.errors import InvalidOrderStateError
from candy_store.sales.domain.model import Order
from candy_store.sales.domain.value_objects import (
    CancellationReason,
    OrderId,
    OrderStatus,
    PaymentMethod,
    SalesChannel,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


@dataclass(frozen=True, slots=True)
class ProcessPosSaleCommand:
    # Lo genera la caja: reenviar la misma venta (timeout, doble click) no la duplica.
    order_id: OrderId
    items: Sequence[ItemQuantity]
    payment_method: PaymentMethod


class ProcessPosSaleUseCase:
    """Venta de mostrador: precio del catálogo, descuento inmediato de stock del salón."""

    def __init__(
        self,
        uow: SalesUnitOfWork,
        stock: StockGateway,
        catalog: ProductCatalog,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._stock = stock
        self._catalog = catalog
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: ProcessPosSaleCommand) -> OrderView:
        order = await find_order(self._uow, command.order_id)
        if order is None:
            order = Order.place(
                order_id=command.order_id,
                channel=SalesChannel.COUNTER,
                lines=await price_lines(self._catalog, command.items),
                placed_at=self._clock.now(),
            )
            await add_order(self._uow, order)

        if order.channel is not SalesChannel.COUNTER:
            raise InvalidOrderStateError(f"La orden {order.order_id} no es de mostrador")
        if order.status is OrderStatus.COMPLETED:
            return OrderView.of(order)
        if order.status is not OrderStatus.PLACED:
            raise InvalidOrderStateError(f"La orden {order.order_id} está {order.status}")

        try:
            await self._stock.take_for_counter_sale(order.order_id, stock_items(order))
        except OutOfStockError:
            await update_order(
                self._uow,
                order.order_id,
                lambda o: o.cancel(CancellationReason.OUT_OF_STOCK, self._clock.now()),
            )
            raise

        completed = await update_order(
            self._uow,
            order.order_id,
            lambda o: o.complete_counter_sale(command.payment_method, self._clock.now()),
        )
        return OrderView.of(completed)
