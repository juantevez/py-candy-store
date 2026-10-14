"""Traducción de errores de dominio/aplicación a respuestas HTTP."""

import re
from collections.abc import Mapping

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from candy_store.shared.application.unit_of_work import ConcurrencyError
from candy_store.shared.domain.errors import DomainError
from candy_store.shared.infrastructure.serialization import to_json_value

type ErrorStatusMap = Mapping[type[Exception], int]


def error_code(exc: Exception) -> str:
    """InsufficientStockError -> insufficient_stock."""
    name = type(exc).__name__.removesuffix("Error")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _error_body(exc: Exception) -> dict[str, object]:
    # Atributos públicos del error (ej. requested/available, productos sin stock)
    # como contexto para el cliente. Lo que no es serializable se omite.
    context: dict[str, object] = {}
    for key, value in vars(exc).items():
        if key.startswith("_"):
            continue
        try:
            context[key] = to_json_value(value)
        except TypeError:
            continue
    return {"code": error_code(exc), "detail": str(exc), **context}


def install_error_handlers(app: FastAPI, statuses: ErrorStatusMap) -> None:
    """Registra handlers para errores de negocio. Cada contexto aporta su mapa
    error -> status; se resuelve por herencia. Los errores de dominio no mapeados
    responden 422."""

    def resolve_status(exc: Exception) -> int:
        for cls in type(exc).__mro__:
            if cls in statuses:
                return statuses[cls]
        return status.HTTP_422_UNPROCESSABLE_CONTENT

    async def handle_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=resolve_status(exc), content=_error_body(exc))

    async def handle_concurrency_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "code": "concurrent_modification",
                "detail": "El recurso fue modificado por otra operación; reintentar.",
            },
        )

    app.add_exception_handler(DomainError, handle_error)
    for error_type in statuses:
        if not issubclass(error_type, DomainError):
            app.add_exception_handler(error_type, handle_error)
    app.add_exception_handler(ConcurrencyError, handle_concurrency_error)
