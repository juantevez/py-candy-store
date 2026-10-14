"""Prueba de humo de la API completa contra un entorno levantado (docker compose).

Recorre todos los endpoints con un flujo de negocio realista y verifica cada paso.
Se puede correr las veces que haga falta: usa SKUs, CUIT y remitos nuevos en cada
corrida.

    uv run python tools/smoke_test.py [--api http://localhost:8000] [--mp http://localhost:8099]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

RUN = uuid.uuid4().hex[:5].upper()
# "Hoy" en la zona horaria del comercio, la misma que usa la API para los vencimientos.
TODAY = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()


class Smoke:
    def __init__(self, api: str, mp: str) -> None:
        self.api = httpx.Client(base_url=api, timeout=15)
        self.mp = httpx.Client(base_url=mp, timeout=15)
        self.passed = 0
        self.failed: list[str] = []

    def check(self, description: str, condition: bool, detail: object = "") -> None:
        if condition:
            self.passed += 1
            print(f"  ✓ {description}")
        else:
            self.failed.append(description)
            print(f"  ✗ {description}  {detail}")

    def call(
        self, method: str, path: str, expected: int, json: object = None, **kwargs: Any
    ) -> Any:
        response = self.api.request(method, path, json=json, **kwargs)
        self.check(
            f"{method} {path} -> {expected}",
            response.status_code == expected,
            f"(recibido {response.status_code}: {response.text[:300]})",
        )
        return response.json() if response.content else None

    def wait_for(self, description: str, probe: Callable[[], bool], seconds: float = 10) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if probe():
                self.check(description, True)
                return
            time.sleep(0.5)
        self.check(description, False, f"(no ocurrió en {seconds}s)")

    def stock(self, product_id: str, location: str) -> dict[str, Any]:
        body = self.api.get(f"/inventory/products/{product_id}/stock").json()
        return next((loc for loc in body["locations"] if loc["location"] == location), {})


def random_cuit() -> str:
    while True:
        digits = [3, 0] + [random.randint(0, 9) for _ in range(8)]
        total = sum(d * w for d, w in zip(digits, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2), strict=True))
        check = 11 - total % 11
        check = 0 if check == 11 else check
        if check != 10:
            return "".join(map(str, digits)) + str(check)


def days(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def run(s: Smoke) -> None:
    print("\n1. Salud")
    s.call("GET", "/health", 200)

    print("\n2. Catálogo")
    products: dict[str, str] = {}
    for key, name, price in [
        ("alfajor", "Alfajor triple chocolate", "1500"),
        ("chupetin", "Chupetín frutal", "250"),
        ("chocolate", "Tableta chocolate 100g", "3200"),
    ]:
        body = s.call(
            "POST",
            "/catalog/products",
            201,
            {"sku": f"{key[:3]}-{RUN}", "name": name, "price": price},
        )
        products[key] = body["product_id"]
    kit = s.call(
        "POST",
        "/catalog/products",
        201,
        {
            "sku": f"KIT-{RUN}",
            "name": "Caja Día del Niño",
            "price": "6900",
            "components": [
                {"product_id": products["alfajor"], "quantity": 2},
                {"product_id": products["chupetin"], "quantity": 3},
                {"product_id": products["chocolate"], "quantity": 1},
            ],
        },
    )
    s.check("el kit se creó con su receta", kit["is_kit"] and len(kit["components"]) == 3)
    updated = s.call("PATCH", f"/catalog/products/{products['chupetin']}", 200, {"price": "300"})
    s.check("cambio de precio", updated["price"] == "300.00")
    listed = s.call("GET", "/catalog/products", 200, params={"limit": 500})
    s.check(
        "listado incluye los productos nuevos", sum(p["sku"].endswith(RUN) for p in listed) == 4
    )
    s.call("POST", "/catalog/products", 409, {"sku": f"alf-{RUN}", "name": "Dup", "price": "1"})

    print("\n3. Compras: proveedor, órdenes y remitos")
    supplier = s.call(
        "POST", "/purchasing/suppliers", 201, {"name": f"Mayorista {RUN}", "cuit": random_cuit()}
    )
    s.call("POST", "/purchasing/suppliers", 422, {"name": "X", "cuit": "20-12345678-5"})
    warehouse_po = s.call(
        "POST",
        "/purchasing/orders",
        201,
        {
            "supplier_id": supplier["supplier_id"],
            "location": "warehouse",
            "lines": [
                {"product_id": products["alfajor"], "quantity": 100, "unit_cost": "800"},
                {"product_id": products["chupetin"], "quantity": 200, "unit_cost": "90"},
                {"product_id": products["chocolate"], "quantity": 50, "unit_cost": "1900"},
            ],
        },
    )
    store_po = s.call(
        "POST",
        "/purchasing/orders",
        201,
        {
            "supplier_id": supplier["supplier_id"],
            "location": "store_front",
            "lines": [{"product_id": products["alfajor"], "quantity": 24, "unit_cost": "800"}],
        },
    )
    s.call(
        "POST",
        "/purchasing/orders",
        422,
        {
            "supplier_id": supplier["supplier_id"],
            "lines": [{"product_id": kit["product_id"], "quantity": 1, "unit_cost": "1"}],
        },
    )
    point_of_sale = random.randint(1, 9999)
    mismatch = s.call(
        "POST",
        "/purchasing/remittances",
        422,
        {
            "purchase_order_id": warehouse_po["purchase_order_id"],
            "remittance_number": f"{point_of_sale:04d}-00000001",
            "lines": [
                {
                    "product_id": products["alfajor"],
                    "quantity": 150,
                    "lot_number": "ALF-X",
                    "expiration": days(90),
                }
            ],
        },
    )
    s.check("el cruce informa la diferencia", len(mismatch.get("discrepancies", [])) == 1)
    received = s.call(
        "POST",
        "/purchasing/remittances",
        201,
        {
            "purchase_order_id": warehouse_po["purchase_order_id"],
            "remittance_number": f"{point_of_sale:04d}-00000002",
            "lines": [
                {
                    "product_id": products["alfajor"],
                    "quantity": 40,
                    "lot_number": "ALF-A",
                    "expiration": days(45),
                },
                {
                    "product_id": products["alfajor"],
                    "quantity": 60,
                    "lot_number": "ALF-B",
                    "expiration": days(120),
                },
                {
                    "product_id": products["chupetin"],
                    "quantity": 150,
                    "lot_number": "CHU-A",
                    "expiration": days(300),
                },
                {
                    "product_id": products["chocolate"],
                    "quantity": 50,
                    "lot_number": "CHO-A",
                    "expiration": days(180),
                },
            ],
        },
    )
    s.check(
        "orden parcialmente recibida (faltan chupetines)",
        received["status"] == "partially_received",
    )
    s.call(
        "POST",
        "/purchasing/remittances",
        201,
        {
            "purchase_order_id": store_po["purchase_order_id"],
            "remittance_number": f"{point_of_sale:04d}-00000003",
            "lines": [
                {
                    "product_id": products["alfajor"],
                    "quantity": 24,
                    "lot_number": "ALF-S",
                    "expiration": days(60),
                }
            ],
        },
    )
    s.wait_for(
        "el stock ingresa al depósito y al salón (evento vía outbox)",
        lambda: (
            s.stock(products["alfajor"], "warehouse").get("on_hand") == 100
            and s.stock(products["alfajor"], "store_front").get("on_hand") == 24
        ),
    )
    closed = s.call("POST", f"/purchasing/orders/{warehouse_po['purchase_order_id']}/closure", 200)
    s.check("orden cerrada con faltantes", closed["status"] == "closed")
    s.call("GET", f"/purchasing/orders/{store_po['purchase_order_id']}", 200)
    s.call("GET", "/purchasing/suppliers", 200)

    print("\n4. Inventario: armado de kits, mermas y auditoría")
    assembly = s.call(
        "POST",
        "/inventory/kit-assemblies",
        201,
        {
            "assembly_id": str(uuid.uuid4()),
            "kit_product_id": kit["product_id"],
            "location": "warehouse",
            "quantity": 10,
        },
    )
    s.check(
        "el kit vence con su primer componente (alfajor ALF-A)",
        assembly["expiration"] == days(45),
        assembly.get("expiration"),
    )
    alfajor_lots = {
        b["lot_number"]: b["quantity_on_hand"]
        for b in s.stock(products["alfajor"], "warehouse")["batches"]
    }
    s.check(
        "FEFO: los 20 alfajores salieron del lote que vence antes (ALF-A)",
        alfajor_lots == {"ALF-A": 20, "ALF-B": 60},
        alfajor_lots,
    )
    choco = s.stock(products["chocolate"], "warehouse")
    batch_id = choco["batches"][0]["batch_id"]
    s.call(
        "POST",
        "/inventory/losses",
        204,
        {
            "product_id": products["chocolate"],
            "location": "warehouse",
            "batch_id": batch_id,
            "quantity": 2,
            "reason": "breakage",
        },
    )
    audit = s.call(
        "POST",
        "/inventory/audit-counts",
        200,
        {
            "product_id": products["chocolate"],
            "location": "warehouse",
            "batch_id": batch_id,
            "counted_quantity": 37,
        },
    )
    s.check("auditoría: 50 - 10 (kits) - 2 (rotura) = 38, contados 37", audit["delta"] == -1)

    print("\n5. Venta de mostrador")
    counter_order = str(uuid.uuid4())
    sale = {
        "order_id": counter_order,
        "items": [{"product_id": products["alfajor"], "quantity": 3}],
        "payment_method": "cash",
    }
    sold = s.call("POST", "/sales/counter-sales", 201, sale)
    s.check(
        "venta completada al precio del catálogo",
        sold["status"] == "completed" and sold["total"] == "4500.00",
    )
    s.call("POST", "/sales/counter-sales", 201, sale)
    s.check(
        "reintento de la caja no descuenta dos veces",
        s.stock(products["alfajor"], "store_front")["on_hand"] == 21,
    )
    no_stock = s.call(
        "POST",
        "/sales/counter-sales",
        409,
        {
            "order_id": str(uuid.uuid4()),
            "items": [{"product_id": products["chocolate"], "quantity": 1}],
            "payment_method": "debit_card",
        },
    )
    s.check("sin stock en el salón informa el faltante", no_stock.get("code") == "out_of_stock")

    print("\n6. Checkout online + pago (MercadoPago simulado)")
    online_order = str(uuid.uuid4())
    checkout = s.call(
        "POST",
        "/sales/checkouts",
        201,
        {
            "order_id": online_order,
            "items": [
                {"product_id": kit["product_id"], "quantity": 2},
                {"product_id": products["chocolate"], "quantity": 1},
            ],
            "customer_email": "cliente@example.com",
        },
    )
    s.check("total = 2 kits x 6900 + 1 chocolate x 3200", checkout["total"] == "17000.00")
    s.check("stock del kit reservado", s.stock(kit["product_id"], "warehouse")["reserved"] == 2)
    page = s.mp.get(httpx.URL(checkout["redirect_url"]).path)
    s.check("página de pago del simulador", page.status_code == 200 and "Caja" in page.text)
    paid = s.mp.post(
        f"{httpx.URL(checkout['redirect_url']).path}/pay",
        params={"status": "approved"},
        headers={"accept": "application/json"},
    ).json()
    s.check("webhook firmado aceptado por la API", paid.get("webhook_status") == 200, paid)
    order = s.call("GET", f"/sales/orders/{online_order}", 200)
    s.check("orden pagada", order["status"] == "paid", order.get("status"))
    s.check("stock del kit descontado", s.stock(kit["product_id"], "warehouse")["on_hand"] == 8)

    rejected_order = str(uuid.uuid4())
    rejected_checkout = s.call(
        "POST",
        "/sales/checkouts",
        201,
        {"order_id": rejected_order, "items": [{"product_id": kit["product_id"], "quantity": 1}]},
    )
    s.mp.post(
        f"{httpx.URL(rejected_checkout['redirect_url']).path}/pay",
        params={"status": "rejected"},
        headers={"accept": "application/json"},
    )
    waiting = s.call("GET", f"/sales/orders/{rejected_order}", 200)
    s.check(
        "pago rechazado: la orden sigue esperando (se puede reintentar)",
        waiting["status"] == "awaiting_payment",
    )

    print("\n7. Errores")
    s.call("POST", "/sales/webhooks/mercadopago", 401, params={"data.id": "1", "type": "payment"})
    s.call("GET", f"/sales/orders/{uuid.uuid4()}", 404)
    s.call("POST", "/sales/counter-sales", 422, {"order_id": "no-uuid", "items": []})
    s.call("GET", f"/catalog/products/{uuid.uuid4()}", 404)

    print("\n8. CORS para el front")
    preflight = s.api.options(
        "/sales/checkouts",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    s.check(
        "preflight desde http://localhost:3000",
        preflight.headers.get("access-control-allow-origin") == "http://localhost:3000",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--mp", default="http://localhost:8099")
    args = parser.parse_args()
    smoke = Smoke(args.api, args.mp)
    print(f"Prueba de humo (corrida {RUN}) contra {args.api}")
    try:
        run(smoke)
    except (httpx.HTTPError, KeyError, StopIteration, IndexError) as exc:
        smoke.failed.append(f"error inesperado: {exc!r}")
        print(f"  ✗ error inesperado: {exc!r}")
    print(f"\nResultado: {smoke.passed} OK, {len(smoke.failed)} con error")
    return 1 if smoke.failed else 0


if __name__ == "__main__":
    sys.exit(main())
