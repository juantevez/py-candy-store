from fastapi import APIRouter
from sqlalchemy import text

from candy_store.shared.infrastructure.web.dependencies import SessionFactoryDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session_factory: SessionFactoryDep) -> dict[str, str]:
    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
