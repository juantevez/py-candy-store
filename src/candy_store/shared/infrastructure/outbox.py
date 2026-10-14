"""Transactional outbox: los eventos se guardan en la misma transacción que el
agregado y un relay los publica después. Garantiza entrega at-least-once, por lo
que los suscriptores deben ser idempotentes (deduplicar por `event_id`)."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from candy_store.shared.domain.events import DomainEvent
from candy_store.shared.infrastructure.database import Base
from candy_store.shared.infrastructure.event_bus import EventBus
from candy_store.shared.infrastructure.serialization import serialize_event

logger = logging.getLogger(__name__)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index(
            "ix_outbox_messages_pending",
            "occurred_at",
            postgresql_where="published_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    context: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @classmethod
    def from_event(cls, context: str, event: DomainEvent) -> "OutboxMessage":
        return cls(
            id=event.event_id,
            context=context,
            event_type=event.event_type,
            payload=serialize_event(event),
            occurred_at=event.occurred_at,
            published_at=None,
        )


class OutboxRelay:
    """Publica en el bus los mensajes pendientes, en orden de ocurrencia.

    `FOR UPDATE SKIP LOCKED` permite correr varias instancias sin que publiquen
    el mismo mensaje en paralelo.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_bus: EventBus,
        *,
        batch_size: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus
        self._batch_size = batch_size

    async def publish_pending(self) -> int:
        async with self._session_factory() as session, session.begin():
            messages = (
                await session.scalars(
                    select(OutboxMessage)
                    .where(OutboxMessage.published_at.is_(None))
                    .order_by(OutboxMessage.occurred_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for message in messages:
                # Si un handler falla, la transacción se revierte y el lote se reintenta.
                await self._event_bus.publish(message.event_type, message.payload)
                message.published_at = datetime.now(UTC)
            return len(messages)
