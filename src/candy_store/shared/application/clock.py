from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    """Fuente de tiempo inyectable: el dominio nunca consulta el reloj del sistema."""

    def now(self) -> datetime:
        """Instante actual, con zona horaria."""
        ...

    def today(self) -> date:
        """Fecha calendario local del comercio (define si un lote está vencido)."""
        ...
