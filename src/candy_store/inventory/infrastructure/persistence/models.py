"""Tablas ORM de inventario. Son un detalle de persistencia: el dominio no las conoce."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.shared.infrastructure.database import Base

_LOCATIONS = ", ".join(f"'{location.value}'" for location in StockLocation)


def _item_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["product_id", "location"],
        ["inventory_items.product_id", "inventory_items.location"],
        ondelete="CASCADE",
    )


class InventoryItemRow(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (CheckConstraint(f"location IN ({_LOCATIONS})", name="location_valid"),)

    product_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    location: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column()

    batches: Mapped[list[StockBatchRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    reservations: Mapped[list[StockReservationRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    # Bloqueo optimista: el repositorio asigna la nueva versión y SQLAlchemy emite
    # UPDATE ... WHERE version = <leída>, fallando con StaleDataError si cambió.
    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}  # noqa: RUF012


class StockBatchRow(Base):
    __tablename__ = "inventory_batches"
    __table_args__ = (
        _item_fk(),
        CheckConstraint("quantity_on_hand >= 0", name="quantity_non_negative"),
        Index("ix_inventory_batches_item", "product_id", "location"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    product_id: Mapped[uuid.UUID]
    location: Mapped[str] = mapped_column(String(32))
    lot_number: Mapped[str] = mapped_column(String(64))
    expiration: Mapped[date] = mapped_column(Date)
    quantity_on_hand: Mapped[int]
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StockReservationRow(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        _item_fk(),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # Confirmar o liberar una orden busca por referencia.
        Index("ix_inventory_reservations_reference", "reference"),
        # Para el proceso que libera reservas vencidas.
        Index("ix_inventory_reservations_expires_at", "expires_at"),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    location: Mapped[str] = mapped_column(String(32), primary_key=True)
    reference: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProcessedOperationRow(Base):
    """Registro de idempotencia: una fila por (referencia, operación) ya aplicada."""

    __tablename__ = "inventory_processed_operations"

    reference: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
