import re
from dataclasses import dataclass

from candy_store.catalog.domain.errors import InvalidProductError

_SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,31}$")


@dataclass(frozen=True, slots=True)
class Sku:
    """Código interno del producto (ej. ARCOR-ALF-TRIPLE). Mayúsculas, dígitos y guiones."""

    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip().upper()
        if not _SKU_PATTERN.match(normalized):
            raise InvalidProductError(
                f"SKU inválido {self.value!r}: hasta 32 caracteres, letras, dígitos y guiones"
            )
        object.__setattr__(self, "value", normalized)

    def __str__(self) -> str:
        return self.value
