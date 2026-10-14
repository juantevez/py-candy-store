from decimal import Decimal

import pytest

from candy_store.inventory.domain.value_objects import BatchId
from candy_store.shared.domain.value_objects import CurrencyMismatchError, Money, ProductId


def test_rounds_to_cents_half_up() -> None:
    assert Money.of("10.005").amount == Decimal("10.01")
    assert Money.of("10.004").amount == Decimal("10.00")


def test_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money(10.5)  # type: ignore[arg-type]


def test_arithmetic() -> None:
    assert Money.of(100) + Money.of("50.50") == Money.of("150.50")
    assert Money.of(100) - Money.of(30) == Money.of(70)
    assert Money.of("12.50") * 3 == Money.of("37.50")
    assert 2 * Money.of(10) == Money.of(20)


def test_mixing_currencies_fails() -> None:
    with pytest.raises(CurrencyMismatchError):
        _ = Money.of(1, "ARS") + Money.of(1, "USD")


def test_invalid_currency_code() -> None:
    with pytest.raises(ValueError, match="ISO 4217"):
        Money.of(1, "ars")


def test_typed_ids_with_same_uuid_are_not_equal() -> None:
    product_id = ProductId.new()
    # mypy ya rechaza esta comparación en tiempo de análisis; acá se verifica en runtime.
    assert BatchId(product_id.value) != product_id  # type: ignore[comparison-overlap]
    assert ProductId.from_string(str(product_id)) == product_id
