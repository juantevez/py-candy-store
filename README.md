# Candy Store

Plataforma omnicanal (POS + e-commerce) para un comercio de golosinas.
Monolito modular en Python con arquitectura hexagonal y DDD.

## Contextos

| Contexto     | Responsabilidad                                                        |
|--------------|------------------------------------------------------------------------|
| `catalog`    | Productos, categorías, marcas y kits (productos compuestos)            |
| `inventory`  | Stock por lote y ubicación, FEFO, reservas online, mermas y auditoría  |
| `purchasing` | Órdenes de compra y cruce de remitos                                   |
| `sales`      | POS y venta online (MercadoPago)                                       |

Cada contexto tiene `domain/` → `application/` → `infrastructure/`. Las reglas de
dependencia se verifican con `import-linter` (ver `pyproject.toml`).

## Desarrollo

```bash
uv sync                                  # dependencias
docker compose up -d --wait              # PostgreSQL en localhost:5433 (+ base de tests)
uv run alembic upgrade head              # migraciones

uv run pytest                            # todos los tests
uv run pytest -m "not integration"       # solo unitarios (sin base de datos)
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run lint-imports                      # contratos de arquitectura
```

Nueva migración: `uv run alembic revision --autogenerate -m "descripcion"`.
Configuración por variables de entorno con prefijo `CANDY_` (ver `.env.example`).

## Decisiones de diseño

- **Bloqueo optimista** por agregado (`version`): ventas concurrentes del mismo
  producto no pueden pisarse; la perdedora recibe `ConcurrencyError`.
- **Transactional outbox**: los eventos de dominio se guardan en `outbox_messages`
  en la misma transacción que el agregado; `OutboxRelay` los publica después
  (entrega at-least-once, los suscriptores deben ser idempotentes).
- **Cantidades enteras** en la unidad base del producto (unidades, o gramos para
  golosinas sueltas).
