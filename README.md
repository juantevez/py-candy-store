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

uv run uvicorn candy_store.main:create_app --factory --reload   # API en :8000, docs en /docs
```

Nueva migración: `uv run alembic revision --autogenerate -m "descripcion"`.
Configuración por variables de entorno con prefijo `CANDY_` (ver `.env.example`).

## Comunicación entre contextos

Los contextos no se importan entre sí. La única excepción explícita (verificada
por `import-linter`) es que un adaptador puede usar la **API pública** de otro
contexto:

```
sales.application ──► StockGateway / ProductCatalog (puertos de sales, en su lenguaje)
                           ▲
sales.infrastructure.{inventory,catalog}  (anti-corruption layers)
                           │
                           ▼
        inventory.public.InventoryFacade / catalog.public.CatalogFacade
```

Inventory → sales es asincrónico: cuando vence una reserva, el evento
`StockReservationReleased` (vía outbox) hace que sales cancele la orden.

- **Sincrónico** cuando sales necesita la respuesta en el acto: venta de mostrador,
  reserva en el checkout, confirmación con el pago.
- **Todo o nada** por venta/orden, aunque tenga varios productos.
- **Idempotente** por id de orden: reintentos y webhooks duplicados no descuentan dos veces.
- **Eventos (outbox)** para lo que puede ser asincrónico: avisos y trazabilidad
  (cada `StockDeducted` lleva la referencia de la venta y los lotes que salieron).

## API

| Método | Ruta                                              | Uso                                      |
|--------|---------------------------------------------------|------------------------------------------|
| POST   | `/catalog/products`                               | Alta de producto (SKU, nombre, precio)   |
| GET    | `/catalog/products`, `/catalog/products/{id}`     | Consulta del catálogo                    |
| PATCH  | `/catalog/products/{id}`                          | Cambio de nombre, precio o activo        |
| PUT    | `/catalog/products/{id}/components`               | Cambio de la receta de un kit            |
| POST   | `/inventory/batches`                              | Ingreso de un lote                       |
| POST   | `/inventory/losses`                               | Merma, rotura o baja por vencimiento     |
| POST   | `/inventory/audit-counts`                         | Corrección por conteo físico             |
| POST   | `/inventory/kit-assemblies`                       | Armado de kits pre-armados               |
| GET    | `/inventory/products/{product_id}/stock`          | Stock por ubicación y lote               |
| POST   | `/sales/counter-sales`                            | Venta de mostrador (descuenta del salón) |
| POST   | `/sales/checkouts`                                | Checkout web: reserva + pago MercadoPago |
| GET    | `/sales/orders/{order_id}`                        | Estado de una orden                      |
| POST   | `/sales/webhooks/mercadopago`                     | Notificaciones de pago (firmadas)        |

Las salidas de stock solo ocurren a través de ventas. `order_id` lo genera el
cliente (caja o frontend): reenviar el mismo pedido no lo duplica.

Los errores de negocio responden `{"code", "detail", ...}`: 409 para conflictos
(stock, estado de la orden, concurrencia), 404 para recursos inexistentes, 502 si
MercadoPago no responde, 422 para el resto.

## Kits pre-armados

Un kit es un producto del catálogo con receta (`components` al crearlo). No se
anidan kits. Se arman de antemano con `POST /inventory/kit-assemblies`:

- Consume los componentes por FEFO en la ubicación indicada y crea un lote del
  kit, todo en una transacción (si falta un componente, no se mueve nada).
- El lote del kit **vence con su primer componente**: es la fecha a imprimir en la caja.
- No usa stock reservado para órdenes online.
- Queda registrado qué lotes de cada componente entraron en cada lote de kits
  (evento `KitsAssembled`), para responder ante un retiro de mercadería.
- Una vez armado se vende como cualquier producto. Cambiar la receta solo afecta
  a los kits que se armen después.

## Ciclo de vida de una orden

```
Mostrador: placed ──► completed                 (cancelled si no hay stock)
Online:    placed ──► awaiting_payment ──► paid
                 │            │
                 │            ├──► cancelled        (venció la reserva sin pago aprobado)
                 │            └──► refund_required  (pago aprobado sin stock / monto distinto)
                 └──► cancelled                     (sin stock o falla de MercadoPago)
```

Un pago rechazado no cancela la orden: en Checkout Pro el comprador puede
reintentar con otro medio mientras la reserva siga vigente. La preferencia de
pago vence junto con la reserva de stock.

## MercadoPago

Variables (`.env.example`): `CANDY_MERCADOPAGO_ACCESS_TOKEN`,
`CANDY_MERCADOPAGO_WEBHOOK_SECRET` (panel de MercadoPago > Webhooks),
`CANDY_PUBLIC_BASE_URL` (URL pública de esta API, para las notificaciones) y
`CANDY_STOREFRONT_URL` (a dónde vuelve el comprador). Sin la clave secreta del
webhook se rechazan todas las notificaciones.

El webhook valida la firma `x-signature` y nunca confía en el contenido de la
notificación: consulta el pago a la API de MercadoPago y verifica que el monto
coincida con el total de la orden.

## Decisiones de diseño

- **Bloqueo optimista** por agregado (`version`): ventas concurrentes del mismo
  producto no pueden pisarse; la perdedora recibe `ConcurrencyError`.
- **Transactional outbox**: los eventos de dominio se guardan en `outbox_messages`
  en la misma transacción que el agregado; `OutboxRelay` los publica después
  (entrega at-least-once, los suscriptores deben ser idempotentes).
- **Reintento automático** (`retry_on_concurrency`) de los casos de uso ante
  conflictos de bloqueo optimista; si se agotan, la API responde 409.
- **Tareas de fondo** en el proceso de la app: relay del outbox y liberación de
  reservas vencidas (`CANDY_BACKGROUND_TASKS_ENABLED=false` para desactivarlas).
- **Cantidades enteras** en la unidad base del producto (unidades, o gramos para
  golosinas sueltas).
