"""Servicio de Dominio: selección de lotes FEFO (First Expired, First Out)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING

from candy_store.inventory.domain.errors import InsufficientStockError, InvalidQuantityError
from candy_store.inventory.domain.value_objects import BatchAllocation

if TYPE_CHECKING:
    from candy_store.inventory.domain.model import StockBatch


def allocate_fefo(
    batches: Iterable[StockBatch], quantity: int, today: date
) -> tuple[BatchAllocation, ...]:
    """Reparte `quantity` entre los lotes vendibles, primero los que vencen antes.

    Los lotes vencidos o vacíos se ignoran. A igual vencimiento, sale primero el
    que ingresó antes (FIFO como desempate). Es una función pura: no modifica
    los lotes, solo calcula el plan de salida.
    """
    if quantity <= 0:
        raise InvalidQuantityError(f"La cantidad debe ser positiva: {quantity}")

    candidates = sorted(
        (batch for batch in batches if batch.is_sellable(today)),
        key=lambda batch: (batch.expiration, batch.received_at),
    )

    allocations: list[BatchAllocation] = []
    remaining = quantity
    for batch in candidates:
        if remaining == 0:
            break
        taken = min(batch.quantity_on_hand, remaining)
        allocations.append(
            BatchAllocation(
                batch_id=batch.batch_id,
                lot_number=batch.lot_number,
                expiration=batch.expiration,
                quantity=taken,
            )
        )
        remaining -= taken

    if remaining > 0:
        raise InsufficientStockError(requested=quantity, available=quantity - remaining)
    return tuple(allocations)
