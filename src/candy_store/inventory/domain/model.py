"""Agregado InventoryItem: stock de un producto en una ubicación, por lotes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from candy_store.inventory.domain.errors import (
    BatchNotFoundError,
    DuplicateBatchError,
    DuplicateReservationError,
    ExpiredBatchReceivedError,
    InsufficientStockError,
    InvalidKitAssemblyError,
    InvalidQuantityError,
    ReservationNotFoundError,
)
from candy_store.inventory.domain.events import (
    KitsAssembled,
    StockAdjusted,
    StockBatchReceived,
    StockConsumedForKits,
    StockDeducted,
    StockReservationReleased,
    StockReserved,
)
from candy_store.inventory.domain.services import allocate_fefo
from candy_store.inventory.domain.value_objects import (
    AdjustmentReason,
    BatchAllocation,
    BatchId,
    ComponentUsage,
    ExpirationDate,
    LotNumber,
    StockLocation,
    StockReference,
)
from candy_store.shared.domain.aggregate import AggregateRoot
from candy_store.shared.domain.value_objects import ProductId


def _require_positive(quantity: int) -> None:
    if quantity <= 0:
        raise InvalidQuantityError(f"La cantidad debe ser positiva: {quantity}")


class StockBatch:
    """Lote de fabricación físicamente presente en una ubicación.

    Las cantidades se expresan en la unidad base del producto (unidades, o
    gramos para golosinas sueltas por peso). Solo el agregado lo modifica.
    """

    def __init__(
        self,
        *,
        batch_id: BatchId,
        lot_number: LotNumber,
        expiration: ExpirationDate,
        quantity_on_hand: int,
        received_at: datetime,
    ) -> None:
        if quantity_on_hand < 0:
            raise InvalidQuantityError("El stock de un lote no puede ser negativo")
        self.batch_id = batch_id
        self.lot_number = lot_number
        self.expiration = expiration
        self.quantity_on_hand = quantity_on_hand
        self.received_at = received_at

    def is_expired(self, today: date) -> bool:
        return self.expiration.is_expired(today)

    def is_sellable(self, today: date) -> bool:
        return self.quantity_on_hand > 0 and not self.is_expired(today)

    def remove(self, quantity: int) -> None:
        _require_positive(quantity)
        if quantity > self.quantity_on_hand:
            raise InsufficientStockError(requested=quantity, available=self.quantity_on_hand)
        self.quantity_on_hand -= quantity

    def set_counted_quantity(self, quantity: int) -> None:
        if quantity < 0:
            raise InvalidQuantityError("El conteo no puede ser negativo")
        self.quantity_on_hand = quantity

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StockBatch) and other.batch_id == self.batch_id

    def __hash__(self) -> int:
        return hash(self.batch_id)


@dataclass(slots=True)
class StockReservation:
    """Stock apartado para un checkout online pendiente de pago.

    No está asociada a lotes: los lotes se eligen por FEFO recién al confirmar.
    Retiene stock hasta que se confirma o se libera, aunque ya haya pasado
    `expires_at` (la liberación de vencidas la hace un proceso periódico).
    """

    reference: StockReference
    quantity: int
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class InventoryItem(AggregateRoot):
    """Stock de un producto en una ubicación (salón o depósito).

    Invariante: nunca se descuenta ni se reserva más que el disponible, siendo
    disponible = stock vendible (no vencido) - stock reservado.
    """

    def __init__(
        self,
        *,
        product_id: ProductId,
        location: StockLocation,
        batches: Iterable[StockBatch] = (),
        reservations: Iterable[StockReservation] = (),
        version: int = 0,
    ) -> None:
        super().__init__(version=version)
        self.product_id = product_id
        self.location = location
        self._batches = {batch.batch_id: batch for batch in batches}
        self._reservations = {r.reference: r for r in reservations}

    # --- Consultas -------------------------------------------------------

    @property
    def batches(self) -> tuple[StockBatch, ...]:
        return tuple(self._batches.values())

    @property
    def reservations(self) -> tuple[StockReservation, ...]:
        return tuple(self._reservations.values())

    def quantity_on_hand(self) -> int:
        return sum(batch.quantity_on_hand for batch in self._batches.values())

    def sellable_quantity(self, today: date) -> int:
        return sum(b.quantity_on_hand for b in self._batches.values() if b.is_sellable(today))

    def reserved_quantity(self) -> int:
        return sum(r.quantity for r in self._reservations.values())

    def available_quantity(self, today: date) -> int:
        return max(0, self.sellable_quantity(today) - self.reserved_quantity())

    def has_reservation(self, reference: StockReference) -> bool:
        return reference in self._reservations

    def expired_batches(self, today: date) -> tuple[StockBatch, ...]:
        return tuple(
            b for b in self._batches.values() if b.is_expired(today) and b.quantity_on_hand > 0
        )

    # --- Ingresos --------------------------------------------------------

    def receive_batch(
        self,
        *,
        batch_id: BatchId,
        lot_number: LotNumber,
        expiration: ExpirationDate,
        quantity: int,
        received_at: datetime,
        today: date,
    ) -> None:
        self._add_batch(batch_id, lot_number, expiration, quantity, received_at, today)
        self._record(
            StockBatchReceived(
                product_id=self.product_id,
                location=self.location,
                batch_id=batch_id,
                lot_number=lot_number,
                expiration=expiration,
                quantity=quantity,
            )
        )

    # --- Salidas (FEFO) --------------------------------------------------

    def deduct(
        self, quantity: int, today: date, *, reference: StockReference
    ) -> tuple[BatchAllocation, ...]:
        """Salida inmediata (venta POS): consume stock no reservado aplicando FEFO."""
        _require_positive(quantity)
        available = self.available_quantity(today)
        if quantity > available:
            raise InsufficientStockError(requested=quantity, available=available)
        allocations = self._take(quantity, today)
        self._record_deduction(reference, allocations, from_reservation=False)
        return allocations

    def reserve(
        self,
        *,
        reference: StockReference,
        quantity: int,
        expires_at: datetime,
        today: date,
    ) -> None:
        _require_positive(quantity)
        if reference in self._reservations:
            raise DuplicateReservationError(f"Ya existe una reserva para {reference}")
        available = self.available_quantity(today)
        if quantity > available:
            raise InsufficientStockError(requested=quantity, available=available)
        self._reservations[reference] = StockReservation(
            reference=reference, quantity=quantity, expires_at=expires_at
        )
        self._record(
            StockReserved(
                product_id=self.product_id,
                location=self.location,
                reference=reference,
                quantity=quantity,
                expires_at=expires_at,
            )
        )

    def confirm_reservation(
        self, reference: StockReference, today: date
    ) -> tuple[BatchAllocation, ...]:
        """Pago aprobado: la reserva se convierte en salida real de stock por FEFO.

        Sin pérdidas, el stock reservado está garantizado. Si desde la reserva hubo
        vencimientos o roturas y el vendible quedó por debajo de lo reservado, gana
        quien confirma primero; si ya no alcanza, falla, la reserva queda intacta y
        el llamador decide (ej. devolver el pago).
        """
        reservation = self._get_reservation(reference)
        allocations = self._take(reservation.quantity, today)
        self._record_deduction(reference, allocations, from_reservation=True)
        del self._reservations[reference]
        return allocations

    def release_reservation(self, reference: StockReference) -> None:
        reservation = self._get_reservation(reference)
        self._release(reservation, expired=False)

    def release_expired_reservations(self, now: datetime) -> tuple[StockReference, ...]:
        expired = [r for r in self._reservations.values() if r.is_expired(now)]
        for reservation in expired:
            self._release(reservation, expired=True)
        return tuple(r.reference for r in expired)

    # --- Kits pre-armados ------------------------------------------------

    def consume_for_kits(
        self, quantity: int, today: date, *, reference: StockReference
    ) -> tuple[BatchAllocation, ...]:
        """Componente que sale del stock para armar kits. Respeta las reservas igual
        que una venta: no se arman kits con stock apartado para órdenes online."""
        _require_positive(quantity)
        available = self.available_quantity(today)
        if quantity > available:
            raise InsufficientStockError(requested=quantity, available=available)
        allocations = self._take(quantity, today)
        self._record(
            StockConsumedForKits(
                product_id=self.product_id,
                location=self.location,
                reference=reference,
                allocations=allocations,
            )
        )
        return allocations

    def add_assembled_kits(
        self,
        *,
        batch_id: BatchId,
        lot_number: LotNumber,
        quantity: int,
        components: tuple[ComponentUsage, ...],
        assembled_at: datetime,
        today: date,
        reference: StockReference,
    ) -> ExpirationDate:
        """Ingresa un lote de kits recién armados.

        El kit vence cuando vence el primero de sus componentes: es la fecha que
        corresponde imprimir en la caja.
        """
        expirations = [a.expiration for c in components for a in c.allocations]
        if not expirations:
            raise InvalidKitAssemblyError("Un lote de kits necesita componentes")
        if any(c.product_id == self.product_id for c in components):
            raise InvalidKitAssemblyError("Un kit no puede ser componente de sí mismo")
        expiration = min(expirations)
        self._add_batch(batch_id, lot_number, expiration, quantity, assembled_at, today)
        self._record(
            KitsAssembled(
                product_id=self.product_id,
                location=self.location,
                reference=reference,
                batch_id=batch_id,
                lot_number=lot_number,
                expiration=expiration,
                quantity=quantity,
                components=components,
            )
        )
        return expiration

    # --- Ajustes ---------------------------------------------------------

    def register_loss(self, *, batch_id: BatchId, quantity: int, reason: AdjustmentReason) -> None:
        """Merma o rotura sobre un lote concreto. Refleja un hecho físico, por eso
        se permite aunque deje el disponible por debajo de lo reservado."""
        if reason is AdjustmentReason.AUDIT:
            raise ValueError("Los ajustes de auditoría se registran con register_audit_count")
        batch = self._get_batch(batch_id)
        batch.remove(quantity)
        self._record_adjustment(batch_id, -quantity, reason)

    def register_audit_count(self, *, batch_id: BatchId, counted_quantity: int) -> int:
        """Conteo físico de un lote. Devuelve la diferencia aplicada (+ sobrante / - faltante)."""
        batch = self._get_batch(batch_id)
        delta = counted_quantity - batch.quantity_on_hand
        batch.set_counted_quantity(counted_quantity)
        if delta != 0:
            self._record_adjustment(batch_id, delta, AdjustmentReason.AUDIT)
        return delta

    def write_off_expired(self, today: date) -> int:
        """Da de baja todo el stock de lotes vencidos. Devuelve la cantidad dada de baja."""
        written_off = 0
        for batch in self.expired_batches(today):
            quantity = batch.quantity_on_hand
            batch.remove(quantity)
            self._record_adjustment(batch.batch_id, -quantity, AdjustmentReason.EXPIRED)
            written_off += quantity
        return written_off

    # --- Internos --------------------------------------------------------

    def _add_batch(
        self,
        batch_id: BatchId,
        lot_number: LotNumber,
        expiration: ExpirationDate,
        quantity: int,
        received_at: datetime,
        today: date,
    ) -> None:
        _require_positive(quantity)
        if batch_id in self._batches:
            raise DuplicateBatchError(f"El lote {batch_id} ya fue ingresado")
        if expiration.is_expired(today):
            raise ExpiredBatchReceivedError(
                f"No se puede ingresar el lote {lot_number}: venció el {expiration.value}"
            )
        self._batches[batch_id] = StockBatch(
            batch_id=batch_id,
            lot_number=lot_number,
            expiration=expiration,
            quantity_on_hand=quantity,
            received_at=received_at,
        )

    def _take(self, quantity: int, today: date) -> tuple[BatchAllocation, ...]:
        allocations = allocate_fefo(self._batches.values(), quantity, today)
        for allocation in allocations:
            self._batches[allocation.batch_id].remove(allocation.quantity)
        return allocations

    def _record_deduction(
        self,
        reference: StockReference,
        allocations: tuple[BatchAllocation, ...],
        *,
        from_reservation: bool,
    ) -> None:
        self._record(
            StockDeducted(
                product_id=self.product_id,
                location=self.location,
                reference=reference,
                allocations=allocations,
                from_reservation=from_reservation,
            )
        )

    def _release(self, reservation: StockReservation, *, expired: bool) -> None:
        del self._reservations[reservation.reference]
        self._record(
            StockReservationReleased(
                product_id=self.product_id,
                location=self.location,
                reference=reservation.reference,
                quantity=reservation.quantity,
                expired=expired,
            )
        )

    def _record_adjustment(self, batch_id: BatchId, delta: int, reason: AdjustmentReason) -> None:
        self._record(
            StockAdjusted(
                product_id=self.product_id,
                location=self.location,
                batch_id=batch_id,
                delta=delta,
                reason=reason,
            )
        )

    def _get_batch(self, batch_id: BatchId) -> StockBatch:
        try:
            return self._batches[batch_id]
        except KeyError:
            raise BatchNotFoundError(f"No existe el lote {batch_id}") from None

    def _get_reservation(self, reference: StockReference) -> StockReservation:
        try:
            return self._reservations[reference]
        except KeyError:
            raise ReservationNotFoundError(f"No existe una reserva para {reference}") from None
