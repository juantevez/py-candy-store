"""Registro de todas las tablas ORM, para Alembic y los tests de integración."""

from candy_store.catalog.infrastructure.persistence import models as catalog_models
from candy_store.inventory.infrastructure.persistence import models as inventory_models
from candy_store.sales.infrastructure.persistence import models as sales_models
from candy_store.shared.infrastructure.database import Base
from candy_store.shared.infrastructure.outbox import OutboxMessage

metadata = Base.metadata

__all__ = ["OutboxMessage", "catalog_models", "inventory_models", "metadata", "sales_models"]
