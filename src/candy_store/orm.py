"""Registro de todas las tablas ORM, para Alembic y los tests de integración."""

from candy_store.inventory.infrastructure.persistence import models as inventory_models
from candy_store.shared.infrastructure.database import Base
from candy_store.shared.infrastructure.outbox import OutboxMessage

metadata = Base.metadata

__all__ = ["OutboxMessage", "inventory_models", "metadata"]
