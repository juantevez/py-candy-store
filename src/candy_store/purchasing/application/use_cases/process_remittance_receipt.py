"""Recepción en depósito: cruce del remito contra la orden de compra."""

from collections.abc import Sequence
from dataclasses import dataclass

from candy_store.purchasing.application.ports.repository import PurchasingUnitOfWork
from candy_store.purchasing.domain.errors import (
    DuplicateRemittanceError,
    PurchaseOrderNotFoundError,
)
from candy_store.purchasing.domain.model import RemittanceReceipt
from candy_store.purchasing.domain.value_objects import (
    PurchaseOrderId,
    ReceiptLine,
    RemittanceNumber,
    RemittanceReceiptId,
)
from candy_store.shared.application.clock import Clock
from candy_store.shared.application.retry import retry_on_concurrency


@dataclass(frozen=True, slots=True)
class ProcessRemittanceCommand:
    purchase_order_id: PurchaseOrderId
    remittance_number: str
    lines: Sequence[ReceiptLine]


class ProcessRemittanceReceiptUseCase:
    """Registra el remito, actualiza la orden de compra y emite `RemittanceReceived`
    (que inventory consume para ingresar el stock), todo en una transacción.

    Cargar dos veces el mismo remito para la misma orden devuelve el registro
    original: el número de remito del proveedor es la clave de idempotencia.
    """

    def __init__(self, uow: PurchasingUnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    @retry_on_concurrency()
    async def execute(self, command: ProcessRemittanceCommand) -> RemittanceReceiptId:
        number = RemittanceNumber(command.remittance_number)
        async with self._uow as uow:
            order = await uow.orders.get(command.purchase_order_id)
            if order is None:
                raise PurchaseOrderNotFoundError(
                    f"No existe la orden de compra {command.purchase_order_id}"
                )
            existing = await uow.remittances.find_by_number(order.supplier_id, number)
            if existing is not None:
                if existing.purchase_order_id != order.purchase_order_id:
                    raise DuplicateRemittanceError(
                        f"El remito {number} ya se registró en otra orden de compra"
                    )
                return existing.receipt_id

            receipt = RemittanceReceipt.receive(
                receipt_id=RemittanceReceiptId.new(),
                order=order,
                remittance_number=number,
                lines=command.lines,
                received_at=self._clock.now(),
                today=self._clock.today(),
            )
            await uow.remittances.add(receipt)
            await uow.commit()
        return receipt.receipt_id
