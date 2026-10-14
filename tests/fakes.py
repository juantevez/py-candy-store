import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Self

from candy_store.inventory.application.ports.repository import (
    InventoryOperation,
    InventoryRepository,
    InventoryUnitOfWork,
    OperationLog,
)
from candy_store.inventory.domain.model import InventoryItem
from candy_store.inventory.domain.value_objects import StockLocation, StockReference
from candy_store.sales.application.ports.payment_gateway import (
    CheckoutRequest,
    PaymentDetails,
    PaymentGateway,
    PaymentGatewayError,
    PaymentStatus,
)
from candy_store.sales.application.ports.product_catalog import CatalogItem, ProductCatalog
from candy_store.sales.application.ports.repository import OrderRepository, SalesUnitOfWork
from candy_store.sales.application.ports.stock import (
    ItemQuantity,
    OutOfStockError,
    StockGateway,
    StockHoldLostError,
    UnavailableItem,
)
from candy_store.sales.domain.model import Order
from candy_store.sales.domain.value_objects import CheckoutSession, OrderId
from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.domain.value_objects import Money, ProductId
from candy_store.shared.infrastructure.clock import STORE_TIMEZONE

type _Key = tuple[ProductId, StockLocation]


@dataclass
class FixedClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def today(self) -> date:
        return self.current.astimezone(STORE_TIMEZONE).date()


@dataclass
class InMemoryInventoryRepository(InventoryRepository):
    items: dict[_Key, InventoryItem] = field(default_factory=dict)
    seen: set[_Key] = field(default_factory=set)

    async def get(self, product_id: ProductId, location: StockLocation) -> InventoryItem | None:
        item = self.items.get((product_id, location))
        if item is not None:
            self.seen.add((product_id, location))
        return item

    async def list_by_reservation(self, reference: StockReference) -> list[InventoryItem]:
        found = [item for item in self.items.values() if item.has_reservation(reference)]
        for item in found:
            self.seen.add((item.product_id, item.location))
        return found

    async def add(self, item: InventoryItem) -> None:
        key = (item.product_id, item.location)
        self.items[key] = item
        self.seen.add(key)

    async def keys_with_expired_reservations(self, now: datetime, limit: int) -> list[_Key]:
        return [
            key
            for key, item in self.items.items()
            if any(r.is_expired(now) for r in item.reservations)
        ][:limit]


@dataclass
class InMemoryOperationLog(OperationLog):
    entries: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    async def get(
        self, reference: StockReference, operation: InventoryOperation
    ) -> dict[str, Any] | None:
        return self.entries.get((reference.value, operation.value))

    async def add(
        self, reference: StockReference, operation: InventoryOperation, result: dict[str, Any]
    ) -> None:
        self.entries[(reference.value, operation.value)] = result


class FakeInventoryUnitOfWork(InventoryUnitOfWork):
    """UoW en memoria con semántica transaccional real: lo no confirmado se descarta."""

    def __init__(self) -> None:
        self.items = self._repo = InMemoryInventoryRepository()
        self.operations = self._log = InMemoryOperationLog()
        self.commits = 0
        self.published_events: list[DomainEvent] = []
        self._snapshot: tuple[dict[_Key, InventoryItem], dict[tuple[str, str], Any]] | None = None

    async def __aenter__(self) -> Self:
        self._repo.seen.clear()
        self._snapshot = (copy.deepcopy(self._repo.items), copy.deepcopy(self._log.entries))
        return self

    async def commit(self) -> None:
        self.commits += 1
        for key in self._repo.seen:
            self.published_events.extend(self._repo.items[key].pull_events())
        self._snapshot = None

    async def rollback(self) -> None:
        if self._snapshot is not None:
            self._repo.items, self._log.entries = self._snapshot
            self._snapshot = None


# --- Sales -----------------------------------------------------------------------

HOLD_TTL = timedelta(minutes=15)


class FakeSalesUnitOfWork(SalesUnitOfWork):
    def __init__(self) -> None:
        self.orders = self._repo = InMemoryOrderRepository()
        self.commits = 0
        self.published_events: list[DomainEvent] = []
        self._snapshot: dict[OrderId, Order] | None = None

    async def __aenter__(self) -> Self:
        self._repo.seen.clear()
        self._snapshot = copy.deepcopy(self._repo.items)
        return self

    async def commit(self) -> None:
        self.commits += 1
        for order_id in self._repo.seen:
            self.published_events.extend(self._repo.items[order_id].pull_events())
        self._snapshot = None

    async def rollback(self) -> None:
        if self._snapshot is not None:
            self._repo.items = self._snapshot
            self._snapshot = None


@dataclass
class InMemoryOrderRepository(OrderRepository):
    items: dict[OrderId, Order] = field(default_factory=dict)
    seen: set[OrderId] = field(default_factory=set)

    async def get(self, order_id: OrderId) -> Order | None:
        order = self.items.get(order_id)
        if order is not None:
            self.seen.add(order_id)
        return order

    async def add(self, order: Order) -> None:
        self.items[order.order_id] = order
        self.seen.add(order.order_id)


class FakeStockGateway(StockGateway):
    """Stock en memoria con la misma semántica que inventory: todo o nada e
    idempotente por orden."""

    def __init__(self, available: dict[ProductId, int], clock: FixedClock) -> None:
        self.available = dict(available)
        self.holds: dict[OrderId, list[ItemQuantity]] = {}
        self.taken: dict[OrderId, list[ItemQuantity]] = {}
        self._clock = clock

    async def take_for_counter_sale(self, order_id: OrderId, items: Sequence[ItemQuantity]) -> None:
        if order_id in self.taken:
            return
        self._take(items)
        self.taken[order_id] = list(items)

    async def hold_for_online_order(
        self, order_id: OrderId, items: Sequence[ItemQuantity]
    ) -> datetime:
        if order_id not in self.holds:
            self._take(items)
            self.holds[order_id] = list(items)
        return self._clock.now() + HOLD_TTL

    async def commit_online_order(self, order_id: OrderId) -> None:
        if order_id in self.taken:
            return
        if order_id not in self.holds:
            raise StockHoldLostError(f"Sin reserva para {order_id}")
        self.taken[order_id] = self.holds.pop(order_id)

    async def cancel_online_order(self, order_id: OrderId) -> None:
        self.expire(order_id)

    def expire(self, order_id: OrderId) -> None:
        for item in self.holds.pop(order_id, []):
            self.available[item.product_id] += item.quantity

    def _take(self, items: Sequence[ItemQuantity]) -> None:
        missing = tuple(
            UnavailableItem(i.product_id, i.quantity, self.available.get(i.product_id, 0))
            for i in items
            if i.quantity > self.available.get(i.product_id, 0)
        )
        if missing:
            raise OutOfStockError(missing)
        for item in items:
            self.available[item.product_id] -= item.quantity


class FakePaymentGateway(PaymentGateway):
    def __init__(self) -> None:
        self.checkouts: list[CheckoutRequest] = []
        self.payments: dict[str, PaymentDetails] = {}
        self.fail_checkout = False

    async def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        if self.fail_checkout:
            raise PaymentGatewayError("MercadoPago no responde")
        self.checkouts.append(request)
        return CheckoutSession(
            checkout_id=f"pref-{request.order_id}",
            redirect_url=f"https://mp.test/checkout/{request.order_id}",
        )

    async def get_payment(self, payment_id: str) -> PaymentDetails:
        try:
            return self.payments[payment_id]
        except KeyError:
            raise PaymentGatewayError(f"Pago {payment_id} inexistente") from None

    def register(
        self,
        payment_id: str,
        order_id: OrderId | None,
        amount: Money,
        status: PaymentStatus = PaymentStatus.APPROVED,
    ) -> str:
        self.payments[payment_id] = PaymentDetails(payment_id, status, order_id, amount)
        return payment_id


class FakeProductCatalog(ProductCatalog):
    def __init__(self, *items: CatalogItem) -> None:
        self.items = {item.product_id: item for item in items}

    async def find(self, product_ids: Iterable[ProductId]) -> dict[ProductId, CatalogItem]:
        return {pid: self.items[pid] for pid in product_ids if pid in self.items}
