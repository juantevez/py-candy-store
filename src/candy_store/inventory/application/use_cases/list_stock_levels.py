from candy_store.inventory.application.ports.stock_queries import StockLevel, StockQueries
from candy_store.inventory.domain.value_objects import StockLocation
from candy_store.shared.application.clock import Clock


class ListStockLevelsUseCase:
    """Stock de todos los productos en una ubicación (ej. el salón, para el mostrador)."""

    def __init__(self, queries: StockQueries, clock: Clock) -> None:
        self._queries = queries
        self._clock = clock

    async def execute(self, location: StockLocation) -> list[StockLevel]:
        return await self._queries.levels_at(location, self._clock.today())
