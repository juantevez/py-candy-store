"""Traducción de errores de dominio/aplicación a respuestas HTTP."""

import re
from collections.abc import Mapping

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from candy_store.shared.application.unit_of_work import ConcurrencyError
from candy_store.shared.domain.errors import DomainError

type ErrorStatusMap = Mapping[type[Exception], int]


def error_code(exc: Exception) -> str:
    """InsufficientStockError -> insufficient_stock."""
    name = type(exc).__name__.removesuffix("Error")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _error_body(exc: Exception) -> dict[str, object]:
    # Atributos primitivos del error (ej. requested/available) como contexto para el cliente.
    context = {
        key: value
        for key, value in vars(exc).items()
        if not key.startswith("_") and isinstance(value, str | int | float | bool)
    }
    return {"code": error_code(exc), "detail": str(exc), **context}


def install_error_handlers(app: FastAPI, statuses: ErrorStatusMap) -> None:
    """Registra handlers para errores de dominio. Cada contexto aporta su mapa
    error -> status; se resuelve por herencia y por defecto se responde 422."""

    def resolve_status(exc: Exception) -> int:
        for cls in type(exc).__mro__:
            if cls in statuses:
                return statuses[cls]
        return status.HTTP_422_UNPROCESSABLE_CONTENT

    async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=resolve_status(exc), content=_error_body(exc))

    async def handle_concurrency_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "code": "concurrent_modification",
                "detail": "El recurso fue modificado por otra operación; reintentar.",
            },
        )

    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(ConcurrencyError, handle_concurrency_error)
