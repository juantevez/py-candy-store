import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

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

    __mapper_args__ = {"version_id_col": version, "version_id_generator": False}  # noqa: RUF012
