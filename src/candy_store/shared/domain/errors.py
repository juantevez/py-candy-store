class DomainError(Exception):
    """Violación de una regla de negocio. Los adaptadores la traducen (ej. HTTP 409/422)."""
