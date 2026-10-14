"""Serialización de eventos de dominio a JSON para el outbox."""

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from candy_store.shared.domain.events import DomainEvent

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


def to_json_value(value: Any) -> JsonValue:
    """Convierte objetos de dominio a tipos JSON.

    Los objetos de valor de un solo campo `value` (IDs, LotNumber, ExpirationDate)
    se aplanan a su valor primitivo; el resto de los dataclasses se vuelven objetos.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if isinstance(value, UUID | Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        dc_fields = fields(value)
        if len(dc_fields) == 1 and dc_fields[0].name == "value":
            return to_json_value(getattr(value, "value"))  # noqa: B009
        return {f.name: to_json_value(getattr(value, f.name)) for f in dc_fields}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json_value(item) for item in value]
    raise TypeError(f"No se puede serializar {type(value).__name__}")


def serialize_event(event: DomainEvent) -> dict[str, JsonValue]:
    payload = to_json_value(event)
    assert isinstance(payload, dict)
    return payload
