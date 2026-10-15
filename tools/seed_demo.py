"""Carga un catálogo de demostración usando la propia API (no toca la base directo).

Crea productos, un kit, un proveedor, órdenes de compra y remitos con lotes para el
salón y el depósito, y arma algunos kits. Se puede correr más de una vez: los
productos que ya existen (por SKU) no se vuelven a cargar.

    uv run python tools/seed_demo.py [--api http://localhost:8000]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TODAY = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()


@dataclass(frozen=True)
class Stock:
    quantity: int
    expires_in_days: int


@dataclass(frozen=True)
class DemoProduct:
    sku: str
    name: str
    price: str
    cost: str
    store_front: Stock
    warehouse: Stock


PRODUCTS = [
    DemoProduct(
        "DEMO-ALF-TRI",
        "Alfajor triple chocolate negro",
        "1500",
        "820",
        Stock(40, 50),
        Stock(120, 95),
    ),
    DemoProduct("DEMO-ALF-MAI", "Alfajor de maicena", "900", "430", Stock(30, 20), Stock(60, 40)),
    DemoProduct("DEMO-CHU-FRU", "Chupetín frutal", "250", "95", Stock(120, 300), Stock(300, 330)),
    DemoProduct(
        "DEMO-CAR-MAS", "Caramelos masticables x 10", "600", "260", Stock(50, 240), Stock(100, 260)
    ),
    DemoProduct("DEMO-CHI-MEN", "Chicles de menta x 5", "400", "170", Stock(60, 365), Stock(0, 0)),
    DemoProduct(
        "DEMO-CHO-LEC",
        "Tableta de chocolate con leche 100 g",
        "3200",
        "1750",
        Stock(25, 150),
        Stock(80, 170),
    ),
    DemoProduct("DEMO-TUR-MAN", "Turrón de maní", "700", "310", Stock(45, 120), Stock(90, 140)),
    DemoProduct(
        "DEMO-GOM-OSI", "Gomitas ositos 100 g", "1800", "820", Stock(4, 90), Stock(40, 110)
    ),
    DemoProduct(
        "DEMO-BOM-DDL", "Bombón relleno de dulce de leche", "500", "210", Stock(70, 60), Stock(0, 0)
    ),
    DemoProduct("DEMO-PAS-MEN", "Pastillas de menta", "350", "140", Stock(80, 400), Stock(0, 0)),
    DemoProduct("DEMO-GAR-150", "Garrapiñada 150 g", "2500", "1100", Stock(0, 0), Stock(30, 25)),
]
KIT = ("DEMO-KIT-NIN", "Caja regalo Día del Niño", "6900")
KIT_RECIPE = {"DEMO-ALF-TRI": 2, "DEMO-CHU-FRU": 3, "DEMO-CHO-LEC": 1}
KITS_IN_STORE = 6


def random_cuit() -> str:
    while True:
        digits = [3, 0] + [random.randint(0, 9) for _ in range(8)]
        total = sum(d * w for d, w in zip(digits, (5, 4, 3, 2, 7, 6, 5, 4, 3, 2), strict=True))
        check = 11 - total % 11
        check = 0 if check == 11 else check
        if check != 10:
            return "".join(map(str, digits)) + str(check)


def ok(response: httpx.Response) -> Any:
    if response.is_error:
        sys.exit(f"Error {response.status_code} en {response.request.url}: {response.text}")
    return response.json() if response.content else {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    api = httpx.Client(base_url=parser.parse_args().api, timeout=15)

    existing = {p["sku"]: p for p in ok(api.get("/catalog/products", params={"limit": 500}))}
    new_products = [p for p in PRODUCTS if p.sku not in existing]
    for product in new_products:
        existing[product.sku] = ok(
            api.post(
                "/catalog/products",
                json={"sku": product.sku, "name": product.name, "price": product.price},
            )
        )
        print(f"  + {product.name}")
    if not new_products:
        print("El catálogo de demostración ya estaba cargado.")
        return

    supplier = ok(
        api.post(
            "/purchasing/suppliers", json={"name": "Distribuidora Demo", "cuit": random_cuit()}
        )
    )
    point_of_sale = random.randint(100, 9999)
    for number, location in ((1, "store_front"), (2, "warehouse")):
        stock = {p.sku: getattr(p, location) for p in new_products}
        lines = [p for p in new_products if stock[p.sku].quantity > 0]
        order = ok(
            api.post(
                "/purchasing/orders",
                json={
                    "supplier_id": supplier["supplier_id"],
                    "location": location,
                    "lines": [
                        {
                            "product_id": existing[p.sku]["product_id"],
                            "quantity": stock[p.sku].quantity,
                            "unit_cost": p.cost,
                        }
                        for p in lines
                    ],
                },
            )
        )
        ok(
            api.post(
                "/purchasing/remittances",
                json={
                    "purchase_order_id": order["purchase_order_id"],
                    "remittance_number": f"{point_of_sale:04d}-{number:08d}",
                    "lines": [
                        {
                            "product_id": existing[p.sku]["product_id"],
                            "quantity": stock[p.sku].quantity,
                            "lot_number": f"L{TODAY:%y%m}-{p.sku[-3:]}",
                            "expiration": (
                                TODAY + timedelta(days=stock[p.sku].expires_in_days)
                            ).isoformat(),
                        }
                        for p in lines
                    ],
                },
            )
        )
    print("  + Proveedor, 2 órdenes de compra y 2 remitos (salón y depósito)")

    # El stock entra de forma asincrónica (evento vía outbox).
    alfajor_id = existing["DEMO-ALF-TRI"]["product_id"]
    for _ in range(30):
        levels = ok(api.get("/inventory/stock", params={"location": "store_front"}))
        if any(level["product_id"] == alfajor_id for level in levels):
            break
        time.sleep(0.5)

    if KIT[0] not in existing:
        kit = ok(
            api.post(
                "/catalog/products",
                json={
                    "sku": KIT[0],
                    "name": KIT[1],
                    "price": KIT[2],
                    "components": [
                        {"product_id": existing[sku]["product_id"], "quantity": qty}
                        for sku, qty in KIT_RECIPE.items()
                    ],
                },
            )
        )
        ok(
            api.post(
                "/inventory/kit-assemblies",
                json={
                    "assembly_id": str(uuid.uuid4()),
                    "kit_product_id": kit["product_id"],
                    "location": "store_front",
                    "quantity": KITS_IN_STORE,
                },
            )
        )
        print(f"  + {KIT[1]} ({KITS_IN_STORE} armados en el salón)")
    print("Catálogo de demostración listo.")


if __name__ == "__main__":
    main()
