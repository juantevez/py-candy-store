from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CANDY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://candy:candy@localhost:5433/candy_store"
    database_echo: bool = False
    background_tasks_enabled: bool = True
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 100
    # Reserva de stock del checkout online según el medio de pago.
    reservation_ttl_minutes: int = 15  # Mercado Pago
    transfer_hold_hours: int = 24  # transferencia bancaria
    reservation_sweep_interval_seconds: float = 30.0

    # URL pública de esta API (MercadoPago envía las notificaciones acá).
    public_base_url: str = "http://localhost:8000"
    # URL del frontend de la tienda (a dónde vuelve el comprador después de pagar).
    storefront_url: str = "http://localhost:3000"
    # Orígenes del navegador habilitados para llamar a la API (CORS). En variables de
    # entorno va como JSON: CANDY_CORS_ORIGINS='["https://tienda.com.ar"]'.
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    mercadopago_api_url: str = "https://api.mercadopago.com"
    mercadopago_access_token: str = ""
    # Clave secreta del webhook (panel de MercadoPago > Webhooks). Sin ella se
    # rechazan todas las notificaciones.
    mercadopago_webhook_secret: str = ""
    mercadopago_timeout_seconds: float = 10.0

    # Cuenta para las compras online pagadas por transferencia.
    transfer_alias: str = "CANDY.STORE.DEMO"
    transfer_cbu: str = "0000003100000000000001"
    transfer_holder: str = "Candy Store (cuenta de demostración)"
    transfer_bank: str = "Banco de prueba"


@lru_cache
def get_settings() -> Settings:
    return Settings()
