"""Adaptador HTTP de ventas: mostrador, checkout web y webhook de MercadoPago."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

from candy_store.sales.application.ports.payment_gateway import (
    PaymentGateway,
    PaymentGatewayError,
)
from candy_store.sales.application.ports.product_catalog import ProductCatalog
from candy_store.sales.application.ports.repository import SalesUnitOfWork
from candy_store.sales.application.ports.stock import ItemQuantity, OutOfStockError, StockGateway
from candy_store.sales.application.use_cases.checkout_online_cart import (
    CheckoutOnlineCartCommand,
    CheckoutOnlineCartUseCase,
    HoldPolicy,
)
from candy_store.sales.application.use_cases.confirm_bank_transfer import (
    ConfirmBankTransferCommand,
    ConfirmBankTransferUseCase,
)
from candy_store.sales.application.use_cases.get_order import (
    GetOrderUseCase,
    ListRecentOrdersUseCase,
    OrderView,
)
from candy_store.sales.application.use_cases.handle_payment_webhook import (
    HandlePaymentNotificationUseCase,
)
from candy_store.sales.application.use_cases.process_pos_sale import (
    ProcessPosSaleCommand,
    ProcessPosSaleUseCase,
)
from candy_store.sales.domain.errors import (
    InvalidOrderStateError,
    OrderNotFoundError,
    ProductNotSellableError,
)
from candy_store.sales.domain.value_objects import OrderId, PaymentMethod, SalesChannel
from candy_store.sales.infrastructure.external.mercadopago import verify_webhook_signature
from candy_store.sales.infrastructure.persistence.unit_of_work import SqlAlchemySalesUnitOfWork
from candy_store.shared.domain.value_objects import ProductId
from candy_store.shared.infrastructure.web.dependencies import (
    ClockDep,
    SessionFactoryDep,
    SettingsDep,
)
from candy_store.shared.infrastructure.web.errors import ErrorStatusMap

logger = logging.getLogger(__name__)

ERROR_STATUS: ErrorStatusMap = {
    OrderNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidOrderStateError: status.HTTP_409_CONFLICT,
    OutOfStockError: status.HTTP_409_CONFLICT,
    ProductNotSellableError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    PaymentGatewayError: status.HTTP_502_BAD_GATEWAY,
}

# --- Dependencias (las arma el composition root en app.state) ------------------


def _sales_uow(session_factory: SessionFactoryDep) -> SalesUnitOfWork:
    return SqlAlchemySalesUnitOfWork(session_factory)


def _stock(request: Request) -> StockGateway:
    gateway: StockGateway = request.app.state.stock_gateway
    return gateway


def _catalog(request: Request) -> ProductCatalog:
    catalog: ProductCatalog = request.app.state.product_catalog
    return catalog


def _payments(request: Request) -> PaymentGateway:
    payments: PaymentGateway = request.app.state.payment_gateway
    return payments


UowDep = Annotated[SalesUnitOfWork, Depends(_sales_uow)]
StockDep = Annotated[StockGateway, Depends(_stock)]
CatalogDep = Annotated[ProductCatalog, Depends(_catalog)]
PaymentsDep = Annotated[PaymentGateway, Depends(_payments)]

# --- Schemas -------------------------------------------------------------------


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemIn(_Request):
    product_id: UUID
    quantity: PositiveInt


Items = Annotated[list[ItemIn], Field(min_length=1, max_length=100)]


class CounterSaleRequest(_Request):
    order_id: UUID = Field(description="Generado por la caja; reenviarlo no duplica la venta")
    items: Items
    payment_method: PaymentMethod


class CheckoutRequestIn(_Request):
    order_id: UUID = Field(description="Generado por el frontend; reintentar no duplica la orden")
    items: Items
    customer_email: (
        Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)]
        | None
    ) = None
    payment_method: Literal["mercadopago", "bank_transfer"] = "mercadopago"


class TransferConfirmationIn(_Request):
    bank_reference: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] | None
    ) = Field(default=None, description="Número de operación del banco (opcional)")


class OrderLineOut(BaseModel):
    product_id: UUID
    description: str
    unit_price: Decimal
    quantity: int
    subtotal: Decimal


class OrderResponse(BaseModel):
    order_id: UUID
    channel: str
    status: str
    lines: list[OrderLineOut]
    total: Decimal
    currency: str
    placed_at: datetime
    payment_method: str | None
    checkout_url: str | None
    hold_expires_at: datetime | None
    cancellation_reason: str | None
    refund_reason: str | None

    @classmethod
    def of(cls, view: OrderView) -> OrderResponse:
        return cls(
            order_id=view.order_id.value,
            channel=view.channel,
            status=view.status,
            lines=[
                OrderLineOut(
                    product_id=line.product_id.value,
                    description=line.description,
                    unit_price=line.unit_price.amount,
                    quantity=line.quantity,
                    subtotal=line.subtotal.amount,
                )
                for line in view.lines
            ],
            total=view.total.amount,
            currency=view.total.currency,
            placed_at=view.placed_at,
            payment_method=view.payment_method,
            checkout_url=view.checkout_url,
            hold_expires_at=view.hold_expires_at,
            cancellation_reason=view.cancellation_reason,
            refund_reason=view.refund_reason,
        )


class TransferInstructions(BaseModel):
    """Datos que el comprador necesita para transferir."""

    alias: str
    cbu: str
    holder: str
    bank: str
    amount: Decimal
    currency: str
    reference: str = Field(description="Para indicar en el concepto de la transferencia")


class CheckoutResponse(BaseModel):
    order_id: UUID
    payment_method: str
    redirect_url: str | None = Field(description="Solo Mercado Pago: a dónde redirigir")
    expires_at: datetime
    total: Decimal
    currency: str
    transfer: TransferInstructions | None = Field(description="Solo transferencia")


def _items(items: list[ItemIn]) -> list[ItemQuantity]:
    return [ItemQuantity(ProductId(item.product_id), item.quantity) for item in items]


# --- Endpoints -------------------------------------------------------------------

router = APIRouter(prefix="/sales", tags=["sales"])


@router.post("/counter-sales", status_code=status.HTTP_201_CREATED)
async def counter_sale(
    body: CounterSaleRequest, uow: UowDep, stock: StockDep, catalog: CatalogDep, clock: ClockDep
) -> OrderResponse:
    view = await ProcessPosSaleUseCase(uow, stock, catalog, clock).execute(
        ProcessPosSaleCommand(
            order_id=OrderId(body.order_id),
            items=_items(body.items),
            payment_method=body.payment_method,
        )
    )
    return OrderResponse.of(view)


@router.post("/checkouts", status_code=status.HTTP_201_CREATED)
async def checkout(
    body: CheckoutRequestIn,
    uow: UowDep,
    stock: StockDep,
    catalog: CatalogDep,
    payments: PaymentsDep,
    clock: ClockDep,
    settings: SettingsDep,
) -> CheckoutResponse:
    holds = HoldPolicy(
        mercadopago=timedelta(minutes=settings.reservation_ttl_minutes),
        bank_transfer=timedelta(hours=settings.transfer_hold_hours),
    )
    result = await CheckoutOnlineCartUseCase(
        uow, stock, catalog, payments, clock, holds=holds
    ).execute(
        CheckoutOnlineCartCommand(
            order_id=OrderId(body.order_id),
            items=_items(body.items),
            customer_email=body.customer_email,
            payment_method=PaymentMethod(body.payment_method),
        )
    )
    transfer = None
    if result.payment_method is PaymentMethod.BANK_TRANSFER:
        transfer = TransferInstructions(
            alias=settings.transfer_alias,
            cbu=settings.transfer_cbu,
            holder=settings.transfer_holder,
            bank=settings.transfer_bank,
            amount=result.total.amount,
            currency=result.total.currency,
            reference=transfer_reference(result.order_id),
        )
    return CheckoutResponse(
        order_id=result.order_id.value,
        payment_method=result.payment_method,
        redirect_url=result.redirect_url,
        expires_at=result.expires_at,
        total=result.total.amount,
        currency=result.total.currency,
        transfer=transfer,
    )


def transfer_reference(order_id: OrderId) -> str:
    """Referencia corta para el concepto de la transferencia (identifica la orden)."""
    return f"CANDY-{str(order_id)[:8].upper()}"


@router.post("/orders/{order_id}/transfer-confirmation")
async def confirm_bank_transfer(
    order_id: UUID,
    uow: UowDep,
    stock: StockDep,
    clock: ClockDep,
    body: TransferConfirmationIn | None = None,
) -> OrderResponse:
    """Uso interno: el personal confirma que la transferencia se acreditó."""
    view = await ConfirmBankTransferUseCase(uow, stock, clock).execute(
        ConfirmBankTransferCommand(
            order_id=OrderId(order_id), bank_reference=body.bank_reference if body else None
        )
    )
    return OrderResponse.of(view)


@router.get("/orders")
async def list_recent_orders(
    uow: UowDep,
    channel: SalesChannel | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[OrderResponse]:
    """Últimas órdenes, las más recientes primero (ej. `channel=counter` para la caja)."""
    views = await ListRecentOrdersUseCase(uow).execute(channel=channel, limit=limit)
    return [OrderResponse.of(view) for view in views]


@router.get("/orders/{order_id}")
async def get_order(order_id: UUID, uow: UowDep) -> OrderResponse:
    return OrderResponse.of(await GetOrderUseCase(uow).execute(OrderId(order_id)))


@router.post("/webhooks/mercadopago")
async def mercadopago_webhook(
    request: Request,
    uow: UowDep,
    stock: StockDep,
    payments: PaymentsDep,
    clock: ClockDep,
    settings: SettingsDep,
    data_id: Annotated[str | None, Query(alias="data.id")] = None,
    topic: Annotated[str | None, Query(alias="type")] = None,
    x_signature: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Notificación de MercadoPago. Responde 2xx salvo error de procesamiento, en
    cuyo caso MercadoPago reintenta (el procesamiento es idempotente)."""
    body: dict[str, Any] = {}
    if await request.body():
        try:
            body = await request.json()
        except ValueError:
            body = {}
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    payment_id = data_id or (str(data["id"]) if data and "id" in data else None)

    if not verify_webhook_signature(
        secret=settings.mercadopago_webhook_secret,
        x_signature=x_signature,
        x_request_id=x_request_id,
        data_id=payment_id,
    ):
        logger.warning("Webhook de MercadoPago con firma inválida (data.id=%s)", payment_id)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Firma inválida")

    if (topic or body.get("type")) != "payment" or not payment_id:
        return {"outcome": "ignored"}
    outcome = await HandlePaymentNotificationUseCase(uow, stock, payments, clock).execute(
        payment_id
    )
    return {"outcome": outcome.value}
