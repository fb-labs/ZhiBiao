from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.database import get_db
from zhibiao.models import (
    Account,
    BidDocument,
    BidSection,
    Delivery,
    Review,
    Tender,
    TenderFile,
)
from zhibiao.security import require_permission

router = APIRouter(prefix="/stats", tags=["v2-stats"])


@router.get("/overview")
async def overview(
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    total = await db.scalar(select(func.count()).select_from(Tender)) or 0
    rows = (
        await db.execute(select(Tender.status, func.count()).group_by(Tender.status))
    ).all()
    by_status = {status: count for status, count in rows}

    files = await db.scalar(select(func.count()).select_from(TenderFile)) or 0
    sections = await db.scalar(select(func.count()).select_from(BidSection)) or 0
    reviews = await db.scalar(select(func.count()).select_from(Review)) or 0
    deliveries = await db.scalar(select(func.count()).select_from(Delivery)) or 0
    documents = await db.scalar(select(func.count()).select_from(BidDocument)) or 0

    since = datetime.now() - timedelta(days=180)
    created = (
        await db.execute(
            select(Tender.created_at).where(Tender.created_at >= since)
        )
    ).scalars().all()
    buckets: dict[str, int] = {}
    for created_at in created:
        if not created_at:
            continue
        key = created_at.strftime("%Y-%m")
        buckets[key] = buckets.get(key, 0) + 1
    months = []
    cursor = datetime.now().replace(day=1)
    for _ in range(6):
        key = cursor.strftime("%Y-%m")
        months.append({"month": key, "count": buckets.get(key, 0)})
        cursor = (cursor - timedelta(days=1)).replace(day=1)

    recent = (
        await db.execute(select(Tender).order_by(Tender.created_at.desc()).limit(6))
    ).scalars().all()

    return {
        "total": total,
        "byStatus": by_status,
        "files": files,
        "documents": documents,
        "sections": sections,
        "reviews": reviews,
        "deliveries": deliveries,
        "monthly": list(reversed(months)),
        "recent": [
            {
                "id": row.id,
                "code": row.code,
                "title": row.title,
                "status": row.status,
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in recent
        ],
    }
