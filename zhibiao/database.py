from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zhibiao.settings import get_settings

logger = logging.getLogger(__name__)

_engine = None
_factory: async_sessionmaker[AsyncSession] | None = None
ready = False


def engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        options: dict = {"echo": False, "pool_pre_ping": True}
        if settings.db_type != "sqlite":
            options.update(pool_size=10, max_overflow=5)
        if settings.db_type == "mysql":
            options["pool_recycle"] = 3600
            options["connect_args"] = {"charset": "utf8mb4"}
        _engine = create_async_engine(settings.database_dsn(), **options)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(engine(), class_=AsyncSession, expire_on_commit=False)
    return _factory


async def init_schema() -> None:
    from zhibiao.models import Base

    async with engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def open_database() -> None:
    global ready
    try:
        await init_schema()
        ready = True
        logger.info("v2 数据库初始化完成")
    except Exception as exc:  # noqa: BLE001
        ready = False
        logger.warning("v2 数据库不可用: %s", exc)


async def close_database() -> None:
    global _engine, _factory, ready
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
    ready = False


async def get_db() -> AsyncIterator[AsyncSession]:
    if not ready:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def database_ready() -> bool:
    return ready
