"""Objetos de valor transversales a todos los contextos (Shared Kernel)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Self
from uuid import UUID, uuid4

from candy_store.shared.domain.errors import DomainError

DEFAULT_CURRENCY = "ARS"
_CENTS = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class EntityId:
    """Identificador tipado. Cada subclase es un tipo distinto: ProductId(x) != BatchId(x)."""

    value: UUID

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4())

    @classmethod
    def from_string(cls, raw: str) -> Self:
        return cls(UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


class ProductId(EntityId):
    """Identidad de un producto del catálogo, referenciada por los demás contextos."""


class CurrencyMismatchError(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class Money:
    """Importe monetario con precisión de centavos. Nunca se construye desde float."""

    amount: Decimal
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if isinstance(self.amount, float):
            raise TypeError("Money no acepta float; usar Decimal, int o str")
        amount = Decimal(self.amount).quantize(_CENTS, rounding=ROUND_HALF_UP)
        object.__setattr__(self, "amount", amount)
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            raise ValueError(f"Código de moneda ISO 4217 inválido: {self.currency!r}")

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str = DEFAULT_CURRENCY) -> Money:
        return cls(Decimal(amount), currency)

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Money:
        return cls(Decimal(0), currency)

    def _check_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: int | Decimal) -> Money:
        if isinstance(factor, float):
            raise TypeError("Money no acepta float; usar Decimal o int")
        return Money(self.amount * factor, self.currency)

    __rmul__ = __mul__

    def __lt__(self, other: Money) -> bool:
        self._check_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._check_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._check_currency(other)
        return self.amount >= other.amount

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def __str__(self) -> str:
        return f"{self.currency} {self.amount}"
