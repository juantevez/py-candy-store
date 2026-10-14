from candy_store.shared.domain.errors import DomainError


class InvalidProductError(DomainError):
    pass


class ProductNotFoundError(DomainError):
    pass


class DuplicateSkuError(DomainError):
    pass
