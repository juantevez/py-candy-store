from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from candy_store.shared.infrastructure.database import Base


class CatalogProductRow(Base):
    __tablename__ = "catalog_products"
    __table_args__ = (CheckConstraint("price_amount > 0", name="price_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    price_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_currency: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool]
    version: Mapped[int] = mapped_column()

    # Receta del kit (vacía para productos simples).
    components: Mapped[list[CatalogKitComponentRow]] = relationship(
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="CatalogKitComponentRow.kit_id",
    )

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}  # noqa: RUF012


class CatalogKitComponentRow(Base):
    __tablename__ = "catalog_kit_components"
    __table_args__ = (CheckConstraint("quantity > 0", name="quantity_positive"),)

    kit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_products.id", ondelete="CASCADE"), primary_key=True
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("catalog_products.id"), primary_key=True, index=True
    )
    quantity: Mapped[int]
