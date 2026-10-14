from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from candy_store.shared.infrastructure.database import Base


class SupplierRow(Base):
    __tablename__ = "purchasing_suppliers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    cuit: Mapped[str] = mapped_column(String(11), unique=True)
    active: Mapped[bool]
    version: Mapped[int] = mapped_column()

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}  # noqa: RUF012


class PurchaseOrderRow(Base):
    __tablename__ = "purchasing_orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchasing_suppliers.id"), index=True
    )
    location: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3))
    version: Mapped[int] = mapped_column()

    lines: Mapped[list[PurchaseOrderLineRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}  # noqa: RUF012


class PurchaseOrderLineRow(Base):
    __tablename__ = "purchasing_order_lines"
    __table_args__ = (
        CheckConstraint("quantity_ordered > 0", name="quantity_ordered_positive"),
        CheckConstraint(
            "quantity_received >= 0 AND quantity_received <= quantity_ordered",
            name="quantity_received_in_range",
        ),
        CheckConstraint("unit_cost > 0", name="unit_cost_positive"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchasing_orders.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    quantity_ordered: Mapped[int]
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity_received: Mapped[int]


class RemittanceReceiptRow(Base):
    __tablename__ = "purchasing_receipts"
    __table_args__ = (
        # El mismo remito de un proveedor no se puede registrar dos veces.
        UniqueConstraint("supplier_id", "remittance_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchasing_suppliers.id"))
    remittance_number: Mapped[str] = mapped_column(String(14))
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchasing_orders.id"), index=True
    )
    location: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column()

    lines: Mapped[list[RemittanceReceiptLineRow]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="RemittanceReceiptLineRow.line_number",
    )


class RemittanceReceiptLineRow(Base):
    __tablename__ = "purchasing_receipt_lines"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchasing_receipts.id", ondelete="CASCADE"), primary_key=True
    )
    line_number: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(index=True)
    quantity: Mapped[int]
    lot_number: Mapped[str] = mapped_column(String(64))
    expiration: Mapped[date] = mapped_column(Date)
