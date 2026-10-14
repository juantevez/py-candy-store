from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from candy_store.shared.infrastructure.database import Base


class OrderRow(Base):
    __tablename__ = "sales_orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), index=True)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3))
    # Desnormalizado para reportes; el dominio siempre lo recalcula desde las líneas.
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    customer_email: Mapped[str | None] = mapped_column(String(254))
    payment_method: Mapped[str | None] = mapped_column(String(24))
    checkout_id: Mapped[str | None] = mapped_column(String(128))
    checkout_url: Mapped[str | None] = mapped_column(Text)
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[str | None] = mapped_column(String(64), index=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(32))
    refund_reason: Mapped[str | None] = mapped_column(String(32))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column()

    lines: Mapped[list[OrderLineRow]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="OrderLineRow.line_number"
    )

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}  # noqa: RUF012


class OrderLineRow(Base):
    __tablename__ = "sales_order_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="CASCADE"), primary_key=True
    )
    line_number: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(index=True)
    description: Mapped[str] = mapped_column(String(120))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    quantity: Mapped[int]
