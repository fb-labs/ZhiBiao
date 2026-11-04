from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.api.documents import document_of
from zhibiao.database import get_db
from zhibiao.generation.quality import run_quality
from zhibiao.generation.runner import manager, serialize_job
from zhibiao.models import Account, BidSection, Job
from zhibiao.security import require_permission

router = APIRouter(prefix="/tenders", tags=["v2-generation"])
detail_router = APIRouter(prefix="/generation", tags=["v2-generation"])


class GenerationCreate(BaseModel):
    mode: str = Field(default="missing")
    sectionIds: list[str] | None = None


@router.post("/{tender_id}/generation")
async def start_generation(
    tender_id: str,
    payload: GenerationCreate,
    account: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    if payload.mode not in {"missing", "retry", "all"}:
        raise HTTPException(status_code=400, detail="非法生成模式")
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=400, detail="请先生成标书大纲")
    try:
        job = await manager.start(
            tender_id,
            account.username,
            mode=payload.mode,
            section_ids=payload.sectionIds,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_job(job)


@router.get("/{tender_id}/generation")
async def generation_state(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    active = await manager.active_job(tender_id)
    rows = (
        await db.execute(
            select(Job)
            .where(Job.tender_id == tender_id, Job.kind == "write")
            .order_by(Job.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    return {
        "active": serialize_job(active) if active else None,
        "items": [serialize_job(row) for row in rows],
    }


@detail_router.get("/{job_id}")
async def get_job(
    job_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Job, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    return serialize_job(row)


@detail_router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Job, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="生成任务不存在")
    if not manager.cancel(job_id):
        raise HTTPException(status_code=400, detail="任务已结束，无法取消")
    return {"ok": True}


@router.post("/{tender_id}/generation/postprocess")
async def postprocess(
    tender_id: str,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=400, detail="尚未生成标书大纲")
    rows = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    quality = run_quality(list(rows))
    for section_id, body in quality["updates"].items():
        row = next((item for item in rows if item.id == section_id), None)
        if row:
            row.body = body
    await db.flush()
    return {
        "applied": len(quality["updates"]),
        "stats": quality["stats"],
        "issues": quality["issues"][:60],
    }
