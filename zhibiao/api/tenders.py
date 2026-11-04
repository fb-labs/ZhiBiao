from __future__ import annotations

import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.database import get_db
from zhibiao.domain import TenderStatus
from zhibiao.models import (
    Account,
    Analysis,
    AuditEvent,
    BidDocument,
    BidSection,
    Delivery,
    FixProposal,
    Outline,
    Review,
    ReviewIssue,
    Tender,
    TenderFile,
)
from zhibiao.security import load_account, require_permission

router = APIRouter(prefix="/tenders", tags=["v2-tenders"])


class TenderCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    buyer: str = Field(default="", max_length=255)
    budget: str = Field(default="", max_length=80)
    deadline: str = Field(default="", max_length=40)
    summary: str = ""


class TenderPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    buyer: str | None = None
    budget: str | None = None
    deadline: str | None = None
    summary: str | None = None
    status: str | None = None


def _serialize(tender: Tender) -> dict:
    return {
        "id": tender.id,
        "code": tender.code,
        "title": tender.title,
        "buyer": tender.buyer,
        "status": tender.status,
        "budget": tender.budget,
        "deadline": tender.deadline,
        "summary": tender.summary,
        "meta": tender.meta or {},
        "createdAt": tender.created_at.isoformat() if tender.created_at else None,
        "updatedAt": tender.updated_at.isoformat() if tender.updated_at else None,
    }


async def _new_code(db: AsyncSession) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    for _ in range(8):
        code = f"ZB-{stamp}-{secrets.token_hex(2).upper()}"
        exists = await db.scalar(select(func.count()).select_from(Tender).where(Tender.code == code))
        if not exists:
            return code
    raise HTTPException(status_code=500, detail="无法生成项目编号")


@router.get("")
async def list_tenders(
    q: str = Query(default="", max_length=100),
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    conditions = []
    if q:
        like = f"%{q}%"
        conditions.append(Tender.title.like(like) | Tender.buyer.like(like) | Tender.code.like(like))
    if status:
        conditions.append(Tender.status == status)
    base = select(Tender)
    if conditions:
        base = base.where(*conditions)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.execute(
            base.order_by(Tender.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {"items": [_serialize(row) for row in rows], "total": total or 0}


@router.post("")
async def create_tender(
    payload: TenderCreate,
    account: Account = Depends(require_permission("tender.write")),
    db: AsyncSession = Depends(get_db),
):
    tender = Tender(
        code=await _new_code(db),
        title=payload.title.strip(),
        buyer=payload.buyer.strip(),
        budget=payload.budget.strip(),
        deadline=payload.deadline.strip(),
        summary=payload.summary.strip(),
        status=TenderStatus.INTAKE,
        created_by=account.id,
    )
    db.add(tender)
    db.add(
        AuditEvent(
            actor=account.username,
            action="tender.create",
            target_type="tender",
            target_id=tender.id,
            detail={"title": tender.title},
        )
    )
    await db.flush()
    return _serialize(tender)


@router.get("/{tender_id}")
async def get_tender(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")

    file_count = await db.scalar(
        select(func.count()).select_from(TenderFile).where(TenderFile.tender_id == tender_id)
    )
    analysis = (
        await db.execute(
            select(Analysis)
            .where(Analysis.tender_id == tender_id, Analysis.is_current.is_(True))
            .order_by(Analysis.created_at.desc())
        )
    ).scalar_one_or_none()
    outline = (
        await db.execute(
            select(Outline)
            .where(Outline.tender_id == tender_id, Outline.is_current.is_(True))
            .order_by(Outline.created_at.desc())
        )
    ).scalar_one_or_none()
    document = (
        await db.execute(
            select(BidDocument)
            .where(BidDocument.tender_id == tender_id)
            .order_by(BidDocument.created_at.desc())
        )
    ).scalar_one_or_none()
    section_count = 0
    if document:
        section_count = await db.scalar(
            select(func.count()).select_from(BidSection).where(BidSection.document_id == document.id)
        )
    latest_review = (
        await db.execute(
            select(Review).where(Review.tender_id == tender_id).order_by(Review.created_at.desc())
        )
    ).scalar_one_or_none()

    data = _serialize(tender)
    data.update(
        {
            "fileCount": file_count or 0,
            "hasAnalysis": analysis is not None,
            "hasOutline": outline is not None,
            "documentId": document.id if document else None,
            "sectionCount": section_count or 0,
            "reviewId": latest_review.id if latest_review else None,
        }
    )
    return data


@router.patch("/{tender_id}")
async def patch_tender(
    tender_id: str,
    payload: TenderPatch,
    account: Account = Depends(require_permission("tender.write")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] not in TenderStatus.ALL:
        raise HTTPException(status_code=400, detail="非法项目状态")
    for key, value in updates.items():
        if value is not None:
            setattr(tender, key, value.strip() if isinstance(value, str) else value)
    db.add(
        AuditEvent(
            actor=account.username,
            action="tender.update",
            target_type="tender",
            target_id=tender.id,
            detail=updates,
        )
    )
    await db.flush()
    return _serialize(tender)


@router.delete("/{tender_id}")
async def delete_tender(
    tender_id: str,
    account: Account = Depends(require_permission("tender.write")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")

    document_ids = (
        await db.execute(select(BidDocument.id).where(BidDocument.tender_id == tender_id))
    ).scalars().all()
    if document_ids:
        await db.execute(delete(BidSection).where(BidSection.document_id.in_(document_ids)))

    review_ids = (
        await db.execute(select(Review.id).where(Review.tender_id == tender_id))
    ).scalars().all()
    if review_ids:
        issue_ids = (
            await db.execute(select(ReviewIssue.id).where(ReviewIssue.review_id.in_(review_ids)))
        ).scalars().all()
        if issue_ids:
            await db.execute(delete(FixProposal).where(FixProposal.issue_id.in_(issue_ids)))
        await db.execute(delete(ReviewIssue).where(ReviewIssue.review_id.in_(review_ids)))
        await db.execute(delete(Review).where(Review.id.in_(review_ids)))

    for model in (Delivery, Outline, Analysis, TenderFile, BidDocument):
        await db.execute(delete(model).where(model.tender_id == tender_id))  # type: ignore[attr-defined]

    await db.delete(tender)
    db.add(
        AuditEvent(
            actor=account.username,
            action="tender.delete",
            target_type="tender",
            target_id=tender_id,
            detail={"title": tender.title},
        )
    )
    return {"ok": True}
