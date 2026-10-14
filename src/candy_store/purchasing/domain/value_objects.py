import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from candy_store.purchasing.domain.errors import (
    InvalidPurchaseOrderError,
    InvalidRemittanceError,
    InvalidSupplierError,
)
from candy_store.shared.domain.value_objects import EntityId, Money, ProductId

_CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
_REMITTANCE_PATTERN = re.compile(r"^(\d{4,5})-(\d{8})$")
_MAX_LOT_LENGTH = 64


class SupplierId(EntityId):
    pass


class PurchaseOrderId(EntityId):
    pass


class RemittanceReceiptId(EntityId):
    pass


class DeliveryLocation(StrEnum):
    """Dónde se recibe la mercadería (mismos valores que las ubicaciones de inventario)."""

    WAREHOUSE = "warehouse"
    STORE_FRONT = "store_front"


class PurchaseOrderStatus(StrEnum):
    OPEN = "open"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"  # anulada sin haber recibido nada
    CLOSED = "closed"  # cerrada con faltantes que ya no van a llegar


@dataclass(frozen=True, slots=True)
class Cuit:
    """CUIT argentino (11 dígitos con dígito verificador). Se guarda sin guiones."""

    value: str

    def __post_init__(self) -> None:
        digits = re.sub(r"[\s-]", "", self.value)
        if not re.fullmatch(r"\d{11}", digits):
            raise InvalidSupplierError(f"CUIT inválido {self.value!r}: debe tener 11 dígitos")
        total = sum(int(d) * w for d, w in zip(digits[:10], _CUIT_WEIGHTS, strict=True))
        check = 11 - total % 11
        check = 0 if check == 11 else check
        if check == 10 or check != int(digits[10]):
            raise InvalidSupplierError(f"CUIT inválido {self.value!r}: dígito verificador")
        object.__setattr__(self, "value", digits)

    @property
    def formatted(self) -> str:
        return f"{self.value[:2]}-{self.value[2:10]}-{self.value[10]}"

    def __str__(self) -> str:
        return self.formatted


@dataclass(frozen=True, slots=True)
class RemittanceNumber:
    """Número de remito: punto de venta (4 o 5 dígitos) y número (8 dígitos).
    Se normaliza a 5 dígitos de punto de venta: 0001-00000123 == 00001-00000123."""

    value: str

    def __post_init__(self) -> None:
        match = _REMITTANCE_PATTERN.fullmatch(self.value.strip())
        if not match:
            raise InvalidRemittanceError(
                f"Número de remito inválido {self.value!r}: formato 0001-00012345"
            )
        point_of_sale, number = match.groups()
        object.__setattr__(self, "value", f"{int(point_of_sale):05d}-{number}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class PurchaseOrderLine:
    product_id: ProductId
    quantity_ordered: int
    unit_cost: Money
    quantity_received: int = 0

    def __post_init__(self) -> None:
        if self.quantity_ordered <= 0:
            raise InvalidPurchaseOrderError("La cantidad pedida debe ser positiva")
        if self.unit_cost.amount <= 0:
            raise InvalidPurchaseOrderError("El costo unitario debe ser positivo")
        if not 0 <= self.quantity_received <= self.quantity_ordered:
            raise InvalidPurchaseOrderError("Cantidad recibida fuera de rango")

    @property
    def pending(self) -> int:
        return self.quantity_ordered - self.quantity_received


@dataclass(frozen=True, slots=True)
class ReceiptLine:
    """Una línea del remito: un lote de un producto, tal como llegó físicamente."""

    product_id: ProductId
    quantity: int
    lot_number: str
    expiration: date

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise InvalidRemittanceError("La cantidad recibida debe ser positiva")
        lot = self.lot_number.strip().upper()
        if not lot or len(lot) > _MAX_LOT_LENGTH:
            raise InvalidRemittanceError(f"Número de lote inválido: {self.lot_number!r}")
        object.__setattr__(self, "lot_number", lot)
