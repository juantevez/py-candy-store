"""reservations by reference and idempotency log

Revision ID: 0d02905bb430
Revises: c490462df6bf
Create Date: 2026-10-14 17:46:04.838088

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0d02905bb430"
down_revision: str | Sequence[str] | None = "c490462df6bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inventory_processed_operations",
        sa.Column("reference", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "reference", "operation", name=op.f("pk_inventory_processed_operations")
        ),
    )

    # Las reservas existentes conservan su id anterior como referencia, para no
    # liberar stock retenido por checkouts en curso.
    op.add_column(
        "inventory_reservations", sa.Column("reference", sa.String(length=64), nullable=True)
    )
    op.execute("UPDATE inventory_reservations SET reference = id::text")
    op.alter_column("inventory_reservations", "reference", nullable=False)

    op.drop_constraint("pk_inventory_reservations", "inventory_reservations", type_="primary")
    op.drop_column("inventory_reservations", "id")
    op.create_primary_key(
        "pk_inventory_reservations",
        "inventory_reservations",
        ["product_id", "location", "reference"],
    )
    # El nuevo PK ya cubre las búsquedas por (product_id, location).
    op.drop_index(op.f("ix_inventory_reservations_item"), table_name="inventory_reservations")
    op.create_index(
        "ix_inventory_reservations_reference", "inventory_reservations", ["reference"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_reservations_reference", table_name="inventory_reservations")
    op.create_index(
        op.f("ix_inventory_reservations_item"),
        "inventory_reservations",
        ["product_id", "location"],
        unique=False,
    )
    op.drop_constraint("pk_inventory_reservations", "inventory_reservations", type_="primary")
    op.add_column("inventory_reservations", sa.Column("id", sa.UUID(), nullable=True))
    op.execute("UPDATE inventory_reservations SET id = gen_random_uuid()")
    op.alter_column("inventory_reservations", "id", nullable=False)
    op.create_primary_key("pk_inventory_reservations", "inventory_reservations", ["id"])
    op.drop_column("inventory_reservations", "reference")
    op.drop_table("inventory_processed_operations")
