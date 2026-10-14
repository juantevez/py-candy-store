"""Datos de entrada/salida de las operaciones de stock de varias líneas (una venta u orden)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from candy_store.inventory.domain.errors import InvalidQuantityError
from candy_store.inventory.domain.value_objects import (
    BatchAllocation,
    BatchId,
    ExpirationDate,
    LotNumber,
)
from candy_store.shared.domain.value_objects import ProductId


@dataclass(frozen=True, slots=True)
class StockLine:
    product_id: ProductId
    quantity: int


def normalize_lines(lines: Iterable[StockLine]) -> tuple[StockLine, ...]:
    """Une líneas repetidas del mismo producto y las ordena por producto.

    El orden determinista hace que transacciones concurrentes toquen los
    agregados en la misma secuencia, reduciendo deadlocks.
    """
    totals: dict[ProductId, int] = {}
    for line in lines:
        if line.quantity <= 0:
            raise InvalidQuantityError(f"La cantidad debe ser positiva: {line.quantity}")
        totals[line.product_id] = totals.get(line.product_id, 0) + line.quantity
    if not totals:
        raise InvalidQuantityError("La operación debe tener al menos una línea")
    return tuple(
        StockLine(product_id=pid, quantity=qty)
        for pid, qty in sorted(totals.items(), key=lambda kv: str(kv[0]))
    )


@dataclass(frozen=True, slots=True)
class LineAllocations:
    product_id: ProductId
    allocations: tuple[BatchAllocation, ...]

    @property
    def quantity(self) -> int:
        return sum(a.quantity for a in self.allocations)


@dataclass(frozen=True, slots=True)
class DeductionResult:
    """Qué lotes salieron por cada producto (trazabilidad de la venta)."""

    lines: tuple[LineAllocations, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "lines": [
                {
                    "product_id": str(line.product_id),
                    "allocations": [
                        {
                            "batch_id": str(a.batch_id),
                            "lot_number": a.lot_number.value,
                            "expiration": a.expiration.value.isoformat(),
                            "quantity": a.quantity,
                        }
                        for a in line.allocations
                    ],
                }
                for line in self.lines
            ]
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeductionResult:
        return cls(
            lines=tuple(
                LineAllocations(
                    product_id=ProductId(UUID(line["product_id"])),
                    allocations=tuple(
                        BatchAllocation(
                            batch_id=BatchId(UUID(a["batch_id"])),
                            lot_number=LotNumber(a["lot_number"]),
                            expiration=ExpirationDate(date.fromisoformat(a["expiration"])),
                            quantity=a["quantity"],
                        )
                        for a in line["allocations"]
                    ),
                )
                for line in data["lines"]
            )
        )


@dataclass(frozen=True, slots=True)
class ReservationReceipt:
    expires_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {"expires_at": self.expires_at.isoformat()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ReservationReceipt:
        return cls(expires_at=datetime.fromisoformat(data["expires_at"]))
