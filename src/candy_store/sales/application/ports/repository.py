from abc import ABC, abstractmethod

from candy_store.sales.domain.model import Order
from candy_store.sales.domain.value_objects import OrderId
from candy_store.shared.application.unit_of_work import UnitOfWork


class OrderRepository(ABC):
    @abstractmethod
    async def get(self, order_id: OrderId) -> Order | None: ...

    @abstractmethod
    async def add(self, order: Order) -> None: ...


class SalesUnitOfWork(UnitOfWork):
    orders: OrderRepository
