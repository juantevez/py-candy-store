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

## Entorno local completo (para el front)

```bash
docker compose up -d --build        # base + migraciones + API + MercadoPago simulado
uv run python tools/smoke_test.py   # recorre todos los endpoints (54 verificaciones)
```

| Servicio               | URL                                   |
|------------------------|---------------------------------------|
| API                    | http://localhost:8000                 |
| Swagger / OpenAPI      | http://localhost:8000/docs · `/openapi.json` (para generar el cliente) |
| MercadoPago simulado   | http://localhost:8099                 |
| PostgreSQL             | localhost:5433 (`candy` / `candy`)    |

Notas para integrar el front:

- **CORS** habilitado para `http://localhost:3000` y `:5173` (`CANDY_CORS_ORIGINS`).
- **`order_id` lo genera el front** (UUID v4) al confirmar la venta o iniciar el
  checkout. Si la request falla o hay timeout, reintentar con el **mismo** id: no se
  duplica la venta ni se descuenta stock dos veces.
- **Checkout web:** `POST /sales/checkouts` devuelve `redirect_url`; el front
  redirige ahí. En local es la página del simulador (botones Aprobar / Rechazar /
  Pendiente), que envía el webhook firmado a la API y vuelve a
  `CANDY_STOREFRONT_URL/checkout/{exito|error|pendiente}?payment_id=...&status=...&external_reference=<order_id>`.
  La página de retorno debe consultar `GET /sales/orders/{order_id}` para el estado
  real (el parámetro `status` de la URL es solo informativo).
- **Errores:** `{"code": "...", "detail": "...", ...contexto}`. Ej.: `out_of_stock`
  trae `items` con `requested`/`available` por producto para mostrar al usuario.
- El stock que entra por un remito aparece unos segundos después (procesamiento
  asincrónico).
- La autenticación queda a cargo del gateway / servicio de login: esta API no la valida.

## Desarrollo

```bash
uv sync                                  # dependencias
docker compose up -d --wait postgres     # solo PostgreSQL en localhost:5433 (+ base de tests)
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

Integraciones asincrónicas (eventos vía outbox, suscriptores idempotentes):

- `StockReservationReleased` (inventory → sales): si venció la reserva, sales cancela la orden.
- `RemittanceReceived` (purchasing → inventory): al aceptar un remito, inventory ingresa los lotes.

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
| POST   | `/purchasing/suppliers`, GET `/purchasing/suppliers` | Alta y consulta de proveedores        |
| POST   | `/purchasing/orders`, GET `/purchasing/orders/{id}`  | Órdenes de compra                     |
| POST   | `/purchasing/orders/{id}/cancellation`            | Anular una orden sin recepciones         |
| POST   | `/purchasing/orders/{id}/closure`                 | Cerrar una orden con faltantes           |
| POST   | `/purchasing/remittances`                         | Recepción de un remito en depósito       |
| POST   | `/sales/counter-sales`                            | Venta de mostrador (descuenta del salón) |
| POST   | `/sales/checkouts`                                | Checkout web: Mercado Pago o transferencia |
| POST   | `/sales/orders/{id}/transfer-confirmation`        | Uso interno: confirmar transferencia acreditada |
| GET    | `/sales/orders?channel=counter`                   | Últimas órdenes                          |
| GET    | `/inventory/stock?location=store_front`           | Stock de todos los productos en una ubicación |
| GET    | `/sales/orders/{order_id}`                        | Estado de una orden                      |
| POST   | `/sales/webhooks/mercadopago`                     | Notificaciones de pago (firmadas)        |

Las salidas de stock solo ocurren a través de ventas. `order_id` lo genera el
cliente (caja o frontend): reenviar el mismo pedido no lo duplica.

Los errores de negocio responden `{"code", "detail", ...}`: 409 para conflictos
(stock, estado de la orden, concurrencia), 404 para recursos inexistentes, 502 si
MercadoPago no responde, 422 para el resto.

## Compras y recepción de remitos

1. Se registra el proveedor (CUIT validado) y se emite la orden de compra con
   cantidades y costos. Solo se compran productos del catálogo que no sean kits.
2. Al llegar la mercadería, el depósito carga el remito (`0001-00012345`) con
   **lote y vencimiento** de cada línea; un producto puede venir en varios lotes.
3. **Cruce con la orden:** si trae productos no pedidos o más de lo pendiente, el
   remito se rechaza completo (422 con todas las diferencias). Si trae menos, la
   orden queda `partially_received`. No se acepta mercadería vencida.
4. Aceptado el remito, `RemittanceReceived` hace que inventory ingrese los lotes
   (unos segundos después, vía outbox). Cargar dos veces el mismo remito no duplica nada.
5. Una orden sin recepciones se puede **anular**; una parcialmente recibida cuyos
   faltantes ya no van a llegar se **cierra**.

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

**Transferencia bancaria (online):** el checkout con `payment_method: "bank_transfer"`
reserva el stock por `CANDY_TRANSFER_HOLD_HOURS` (24 h por defecto) y devuelve los datos
de la cuenta (`CANDY_TRANSFER_ALIAS`, `_CBU`, `_HOLDER`, `_BANK`) y una referencia para el
concepto. El personal confirma la acreditación con
`POST /sales/orders/{id}/transfer-confirmation`; si la reserva ya venció, la orden queda
`refund_required` para devolver el dinero.

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
