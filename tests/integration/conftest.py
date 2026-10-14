import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from candy_store.orm import metadata
from candy_store.shared.infrastructure.database import create_session_factory

TEST_DATABASE_URL = os.environ.get(
    "CANDY_TEST_DATABASE_URL",
    "postgresql+asyncpg://candy:candy@localhost:5433/candy_store_test",
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def _can_connect() -> bool:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except OSError:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Aplica las migraciones reales desde cero: también valida que funcionen."""
    if not asyncio.run(_can_connect()):
        pytest.skip(f"Postgres no disponible en {TEST_DATABASE_URL} (docker compose up -d)")
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    # NullPool: pytest-asyncio usa un event loop por test y asyncpg no comparte
    # conexiones entre loops.
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    tables = ", ".join(table.name for table in metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)
