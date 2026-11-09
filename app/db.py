"""Подключение к базе данных."""
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.models import Base


def make_engine(url: str, **kwargs) -> AsyncEngine:
    if url.startswith("sqlite") and ":memory:" not in url:
        # Создаём папку для файла базы, если её нет
        db_path = url.split("///", 1)[-1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
