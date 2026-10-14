FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencias primero (capa cacheada mientras no cambie uv.lock).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
COPY tools ./tools
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uvicorn", "candy_store.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
