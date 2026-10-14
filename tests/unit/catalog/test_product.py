import pytest

from candy_store.catalog.domain.errors import InvalidKitError, InvalidProductError
from candy_store.catalog.domain.events import (
    KitCompositionChanged,
    ProductAvailabilityChanged,
    ProductCreated,
    ProductPriceChanged,
)
from candy_store.catalog.domain.model import Product
from candy_store.catalog.domain.value_objects import KitComponent, Sku
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


# --- Kits ----------------------------------------------------------------------


def make_kit(*components: KitComponent) -> Product:
    return Product.create_kit(
        product_id=ProductId.new(),
        sku=Sku("KIT-NAVIDAD"),
        name="Caja Navidad",
        price=Money.of("9900"),
        components=components,
    )


def test_kit_records_its_recipe() -> None:
    alfajor, turron = ProductId.new(), ProductId.new()

    kit = make_kit(KitComponent(alfajor, 2), KitComponent(turron, 1))

    assert kit.is_kit
    [event] = kit.pull_events()
    assert isinstance(event, ProductCreated)
    assert {(c.product_id, c.quantity) for c in event.components} == {(alfajor, 2), (turron, 1)}


@pytest.mark.parametrize(
    "components",
    [
        (),  # sin componentes
        (KitComponent(ProductId.new(), 1),),  # una sola unidad no es un kit
    ],
)
def test_invalid_recipes(components: tuple[KitComponent, ...]) -> None:
    with pytest.raises(InvalidKitError):
        make_kit(*components)


def test_component_cannot_repeat() -> None:
    alfajor = ProductId.new()
    with pytest.raises(InvalidKitError):
        make_kit(KitComponent(alfajor, 1), KitComponent(alfajor, 1))


def test_component_quantity_must_be_positive() -> None:
    with pytest.raises(InvalidKitError):
        KitComponent(ProductId.new(), 0)


def test_kit_cannot_contain_itself() -> None:
    kit = make_kit(KitComponent(ProductId.new(), 2))
    with pytest.raises(InvalidKitError):
        kit.change_composition([KitComponent(kit.product_id, 2)])


def test_change_composition() -> None:
    kit = make_kit(KitComponent(ProductId.new(), 2))
    kit.pull_events()
    new_recipe = [KitComponent(ProductId.new(), 3), KitComponent(ProductId.new(), 1)]

    kit.change_composition(new_recipe)
    kit.change_composition(list(reversed(new_recipe)))  # mismo contenido: sin evento

    [event] = kit.pull_events()
    assert isinstance(event, KitCompositionChanged)


def test_simple_product_cannot_get_a_recipe() -> None:
    with pytest.raises(InvalidKitError):
        make_product().change_composition([KitComponent(ProductId.new(), 2)])
