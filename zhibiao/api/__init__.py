from __future__ import annotations

from fastapi import APIRouter

from zhibiao.api import (
    accounts,
    ai_admin,
    analysis,
    auth,
    deliveries,
    documents,
    files,
    health,
    outline,
    reviews,
    stats,
    templates,
    tenders,
)

router = APIRouter(prefix="/api")
router.include_router(health.router)
router.include_router(auth.router)
router.include_router(tenders.router)
router.include_router(files.router)
router.include_router(analysis.router)
router.include_router(outline.router)
router.include_router(documents.router)
router.include_router(reviews.router)
router.include_router(reviews.detail_router)
router.include_router(deliveries.router)
router.include_router(deliveries.detail_router)
router.include_router(templates.router)
router.include_router(accounts.router)
router.include_router(ai_admin.router)
router.include_router(stats.router)
