import pytest

from candy_store.catalog.domain.errors import InvalidProductError
from candy_store.catalog.domain.events import (
    ProductAvailabilityChanged,
    ProductCreated,
    ProductPriceChanged,
)
from candy_store.catalog.domain.model import Product
from candy_store.catalog.domain.value_objects import Sku
from candy_store.shared.domain.value_objects import Money, ProductId


def make_product(price: str = "1500") -> Product:
    return Product.create(
        product_id=ProductId.new(),
        sku=Sku("alf-triple"),
        name="Alfajor  triple",
        price=Money.of(price),
    )


def test_create_normalizes_and_records_event() -> None:
    product = make_product()

    assert product.sku == Sku("ALF-TRIPLE")
    assert product.name == "Alfajor triple"
    assert product.active
    [event] = product.pull_events()
    assert isinstance(event, ProductCreated)


@pytest.mark.parametrize("raw", ["", "con espacios", "-EMPIEZA-CON-GUION", "X" * 33, "ñandú"])
def test_invalid_sku(raw: str) -> None:
    with pytest.raises(InvalidProductError):
        Sku(raw)


@pytest.mark.parametrize("price", ["0", "-10"])
def test_price_must_be_positive(price: str) -> None:
    with pytest.raises(InvalidProductError):
        make_product(price)


def test_empty_name_is_rejected() -> None:
    with pytest.raises(InvalidProductError):
        make_product().rename("   ")


def test_price_change_records_old_and_new_price() -> None:
    product = make_product("1500")
    product.pull_events()

    product.change_price(Money.of("1800"))
    product.change_price(Money.of("1800"))  # sin cambio: sin evento

    [event] = product.pull_events()
    assert isinstance(event, ProductPriceChanged)
    assert (event.old_price, event.new_price) == (Money.of("1500"), Money.of("1800"))


def test_deactivate() -> None:
    product = make_product()
    product.pull_events()

    product.set_active(False)
    product.set_active(False)

    assert not product.active
    [event] = product.pull_events()
    assert isinstance(event, ProductAvailabilityChanged)
