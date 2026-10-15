"""Simulador local de MercadoPago (Checkout Pro) para desarrollo y para el front.

Implementa lo que usa la API de Candy Store, con el mismo formato que MercadoPago:

- POST /checkout/preferences       crea la preferencia y devuelve `init_point`
- GET  /checkout/{id}              página de pago con botones Aprobar / Rechazar
- POST /checkout/{id}/pay          paga (status=approved|rejected|pending) y envía el
                                   webhook firmado a `notification_url`
- GET  /v1/payments/{id}           consulta del pago (lo que hace la API al recibir el webhook)
- POST /v1/payments/{id}/notify    reenvía el webhook (MercadoPago reintenta si falla)

Ejecutar:  uv run uvicorn tools.mercadopago_mock:app --port 8099
Variables: MP_MOCK_WEBHOOK_SECRET (igual a CANDY_MERCADOPAGO_WEBHOOK_SECRET) y
           MP_MOCK_PUBLIC_URL (URL del simulador vista desde el navegador).
"""

from __future__ import annotations

import hashlib
import hmac
import html
import itertools
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

WEBHOOK_SECRET = os.environ.get("MP_MOCK_WEBHOOK_SECRET", "dev-webhook-secret")
PUBLIC_URL = os.environ.get("MP_MOCK_PUBLIC_URL", "http://localhost:8099").rstrip("/")
PAYMENT_STATUSES = {"approved", "rejected", "pending"}

logger = logging.getLogger("mercadopago_mock")
app = FastAPI(title="MercadoPago (simulado)", version="mock")

_preferences: dict[str, dict[str, Any]] = {}
_payments: dict[str, dict[str, Any]] = {}
_payment_ids = itertools.count(int(time.time()) % 1_000_000 * 1000)


def _require_token(request: Request) -> None:
    if not request.headers.get("authorization", "").startswith("Bearer "):
        raise HTTPException(401, {"message": "invalid access token", "status": 401})


@app.post("/checkout/preferences", status_code=201)
async def create_preference(request: Request) -> dict[str, Any]:
    _require_token(request)
    body: dict[str, Any] = await request.json()
    pref_id = f"mock-{uuid.uuid4().hex[:12]}"
    total = sum(Decimal(str(i["unit_price"])) * int(i["quantity"]) for i in body["items"])
    _preferences[pref_id] = {**body, "id": pref_id, "total": total}
    return {
        "id": pref_id,
        "init_point": f"{PUBLIC_URL}/checkout/{pref_id}",
        "sandbox_init_point": f"{PUBLIC_URL}/checkout/{pref_id}",
    }


@app.get("/checkout/{pref_id}", response_class=HTMLResponse)
async def checkout_page(pref_id: str) -> str:
    pref = _get_preference(pref_id)
    rows = "".join(
        f"<tr><td>{html.escape(i['title'])}</td><td>{i['quantity']}</td>"
        f"<td>$ {Decimal(str(i['unit_price'])):.2f}</td></tr>"
        for i in pref["items"]
    )
    buttons = "".join(
        f'<form method="post" action="/checkout/{pref_id}/pay?status={s}" style="display:inline">'
        f'<button style="margin:4px;padding:8px 16px">{label}</button></form>'
        for s, label in (
            ("approved", "Aprobar"),
            ("rejected", "Rechazar"),
            ("pending", "Pendiente"),
        )
    )
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>MercadoPago simulado</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:40px auto">
<h1>MercadoPago <small>(simulado)</small></h1>
<p>Orden <code>{html.escape(pref["external_reference"])}</code> — vence
{html.escape(pref.get("expiration_date_to", "-"))}</p>
<table border="1" cellpadding="6" style="border-collapse:collapse">
<tr><th>Producto</th><th>Cant.</th><th>Precio</th></tr>{rows}</table>
<h2>Total: $ {pref["total"]:.2f}</h2>{buttons}</body></html>"""


@app.post("/checkout/{pref_id}/pay", response_model=None)
async def pay(
    pref_id: str, request: Request, status: str = "approved"
) -> Response | dict[str, Any]:
    """Desde la página (form) redirige a las back_urls; con `Accept: application/json`
    devuelve el pago (para automatizar)."""
    pref = _get_preference(pref_id)
    if status not in PAYMENT_STATUSES:
        raise HTTPException(400, f"status debe ser uno de {sorted(PAYMENT_STATUSES)}")
    expires = pref.get("expiration_date_to")
    if expires and datetime.fromisoformat(expires) < datetime.now(UTC):
        raise HTTPException(400, "La preferencia está vencida: ya no acepta pagos")

    payment_id = str(next(_payment_ids))
    _payments[payment_id] = {
        "id": int(payment_id),
        "status": status,
        "external_reference": pref["external_reference"],
        "transaction_amount": float(pref["total"]),
        "currency_id": pref["items"][0].get("currency_id", "ARS"),
        "preference_id": pref_id,
        "notification_url": pref.get("notification_url"),
    }
    webhook_status = await _notify(payment_id)

    if "application/json" in request.headers.get("accept", ""):
        return {**_payments[payment_id], "webhook_status": webhook_status}
    back = pref.get("back_urls", {}).get(
        {"approved": "success", "rejected": "failure"}.get(status, "pending"), PUBLIC_URL
    )
    query = urlencode(
        {
            "payment_id": payment_id,
            "status": status,
            "external_reference": pref["external_reference"],
        }
    )
    separator = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{separator}{query}", status_code=303)


@app.get("/v1/payments/{payment_id}")
async def get_payment(payment_id: str, request: Request) -> dict[str, Any]:
    _require_token(request)
    payment = _payments.get(payment_id)
    if payment is None:
        raise HTTPException(404, {"message": "Payment not found", "status": 404})
    return {k: v for k, v in payment.items() if k != "notification_url"}


@app.post("/v1/payments/{payment_id}/notify")
async def resend_notification(payment_id: str) -> dict[str, Any]:
    if payment_id not in _payments:
        raise HTTPException(404, "Pago inexistente")
    return {"webhook_status": await _notify(payment_id)}


async def _notify(payment_id: str) -> int | None:
    """Envía el webhook firmado igual que MercadoPago (header x-signature)."""
    url = _payments[payment_id].get("notification_url")
    if not url:
        return None
    request_id, ts = str(uuid.uuid4()), str(int(time.time() * 1000))
    manifest = f"id:{payment_id};request-id:{request_id};ts:{ts};"
    signature = hmac.new(WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                params={"data.id": payment_id, "type": "payment"},
                headers={"x-signature": f"ts={ts},v1={signature}", "x-request-id": request_id},
                json={"action": "payment.updated", "type": "payment", "data": {"id": payment_id}},
            )
    except httpx.HTTPError as exc:
        logger.warning("No se pudo enviar el webhook del pago %s: %s", payment_id, exc)
        return None
    logger.info("Webhook del pago %s -> %s", payment_id, response.status_code)
    return response.status_code


def _get_preference(pref_id: str) -> dict[str, Any]:
    pref = _preferences.get(pref_id)
    if pref is None:
        raise HTTPException(404, "Preferencia inexistente")
    return pref
