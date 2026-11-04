from __future__ import annotations

from fastapi import APIRouter

from zhibiao.database import database_ready
from zhibiao.settings import get_settings

router = APIRouter(tags=["v2-health"])


@router.get("/health")
async def health():
    settings = get_settings()
    return {
        "ok": True,
        "version": settings.app_version,
        "dbReady": database_ready(),
        "engine": settings.db_type,
    }
