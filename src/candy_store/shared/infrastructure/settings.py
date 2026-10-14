from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CANDY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://candy:candy@localhost:5433/candy_store"
    database_echo: bool = False
    background_tasks_enabled: bool = True
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 100
    reservation_ttl_minutes: int = 15
    reservation_sweep_interval_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
