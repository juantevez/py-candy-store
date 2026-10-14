from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.domain.errors import PurchaseOrderNotFoundError
from candy_store.purchasing.domain.value_objects import PurchaseOrderId
from candy_store.shared.application.retry import retry_on_concurrency


class CancelPurchaseOrderUseCase:
    def __init__(self, uow: PurchasingUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, purchase_order_id: PurchaseOrderId) -> None:
        async with self._uow as uow:
            order = await uow.orders.get(purchase_order_id)
            if order is None:
                raise PurchaseOrderNotFoundError(f"No existe la orden {purchase_order_id}")
            order.cancel()
            await uow.commit()


class ClosePurchaseOrderUseCase:
    """Cierra una orden parcialmente recibida cuyos faltantes ya no van a llegar."""

    def __init__(self, uow: PurchasingUnitOfWork) -> None:
        self._uow = uow

    @retry_on_concurrency()
    async def execute(self, purchase_order_id: PurchaseOrderId) -> None:
        async with self._uow as uow:
            order = await uow.orders.get(purchase_order_id)
            if order is None:
                raise PurchaseOrderNotFoundError(f"No existe la orden {purchase_order_id}")
            order.close()
            await uow.commit()
