from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.api.analysis import current_analysis
from zhibiao.database import get_db, session_factory
from zhibiao.domain import TenderStatus
from zhibiao.generation.writer import finalize, stream_body
from zhibiao.llm import get_chat_client
from zhibiao.models import Account, BidDocument, BidSection, Tender
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-documents"])


class SectionPatch(BaseModel):
    heading: str | None = Field(default=None, min_length=1, max_length=255)
    intent: str | None = None
    body: str | None = None
    targetWords: int | None = Field(default=None, ge=100, le=8000)


class SectionCreate(BaseModel):
    heading: str = Field(min_length=1, max_length=255)
    intent: str = ""
    targetWords: int = Field(default=800, ge=100, le=8000)
    parentKey: str | None = None


class SectionMove(BaseModel):
    direction: str = Field(default="up")


def serialize_section(row: BidSection) -> dict:
    body = row.body or ""
    return {
        "id": row.id,
        "documentId": row.document_id,
        "position": row.position,
        "parentKey": row.parent_key,
        "heading": row.heading,
        "intent": row.intent,
        "targetWords": row.target_words,
        "body": body,
        "wordCount": len(body.replace(" ", "").replace("\n", "")),
        "status": row.status,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


async def document_of(db: AsyncSession, tender_id: str) -> BidDocument | None:
    return (
        await db.execute(
            select(BidDocument)
            .where(BidDocument.tender_id == tender_id)
            .order_by(BidDocument.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/{tender_id}/document")
async def get_document(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=404, detail="尚未生成标书大纲")
    sections = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    return {
        "id": document.id,
        "tenderId": document.tender_id,
        "title": document.title,
        "status": document.status,
        "sections": [serialize_section(row) for row in sections],
    }


@router.patch("/{tender_id}/sections/{section_id}")
async def patch_section(
    tender_id: str,
    section_id: str,
    payload: SectionPatch,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    document = await document_of(db, tender_id)
    row = await db.get(BidSection, section_id)
    if not document or not row or row.document_id != document.id:
        raise HTTPException(status_code=404, detail="章节不存在")
    updates = payload.model_dump(exclude_unset=True)
    if "heading" in updates and updates["heading"]:
        row.heading = updates["heading"].strip()
    if "intent" in updates and updates["intent"] is not None:
        row.intent = updates["intent"]
    if "body" in updates and updates["body"] is not None:
        row.body = updates["body"]
        row.status = "written" if row.body.strip() else "pending"
    if "targetWords" in updates and updates["targetWords"] is not None:
        row.target_words = int(updates["targetWords"])
    await db.flush()
    return serialize_section(row)


@router.post("/{tender_id}/sections")
async def create_section(
    tender_id: str,
    payload: SectionCreate,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=404, detail="尚未生成标书大纲")
    max_position = await db.scalar(
        select(BidSection.position)
        .where(BidSection.document_id == document.id)
        .order_by(BidSection.position.desc())
    )
    row = BidSection(
        document_id=document.id,
        position=(max_position or 0) + 1,
        parent_key=payload.parentKey,
        heading=payload.heading.strip(),
        intent=payload.intent.strip(),
        target_words=payload.targetWords,
    )
    db.add(row)
    await db.flush()
    return serialize_section(row)


@router.delete("/{tender_id}/sections/{section_id}")
async def delete_section(
    tender_id: str,
    section_id: str,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    document = await document_of(db, tender_id)
    row = await db.get(BidSection, section_id)
    if not document or not row or row.document_id != document.id:
        raise HTTPException(status_code=404, detail="章节不存在")
    await db.delete(row)
    return {"ok": True}


@router.post("/{tender_id}/sections/{section_id}/move")
async def move_section(
    tender_id: str,
    section_id: str,
    payload: SectionMove,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    document = await document_of(db, tender_id)
    row = await db.get(BidSection, section_id)
    if not document or not row or row.document_id != document.id:
        raise HTTPException(status_code=404, detail="章节不存在")
    direction = "up" if payload.direction == "up" else "down"
    comparator = BidSection.position < row.position if direction == "up" else BidSection.position > row.position
    order = BidSection.position.desc() if direction == "up" else BidSection.position.asc()
    neighbour = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id, comparator)
            .order_by(order)
            .limit(1)
        )
    ).scalar_one_or_none()
    if not neighbour:
        return serialize_section(row)
    row.position, neighbour.position = neighbour.position, row.position
    await db.flush()
    return {"ok": True}


@router.post("/{tender_id}/sections/{section_id}/generate")
async def generate_section(
    tender_id: str,
    section_id: str,
    account: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    document = await document_of(db, tender_id)
    section = await db.get(BidSection, section_id)
    if not tender or not document or not section or section.document_id != document.id:
        raise HTTPException(status_code=404, detail="章节不存在")

    analysis = await current_analysis(db, tender_id)
    sections = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()

    async def event_stream() -> AsyncIterator[str]:
        chunks: list[str] = []
        try:
            client = await get_chat_client(assistant="writer")
            async for delta in stream_body(client, tender, analysis, section, list(sections)):
                chunks.append(delta)
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("章节生成失败: %s", exc)
            yield f"data: {json.dumps({'error': f'模型调用失败: {exc}'}, ensure_ascii=False)}\n\n"
            return

        body = finalize("".join(chunks), section.heading, section.target_words)
        if not body:
            yield f"data: {json.dumps({'error': '模型未返回内容'}, ensure_ascii=False)}\n\n"
            return
        async with session_factory()() as session:
            row = await session.get(BidSection, section_id)
            tender_row = await session.get(Tender, tender_id)
            if row and row.body != body:
                row.body = body
                row.status = "written"
            if tender_row and tender_row.status in {
                TenderStatus.OUTLINED,
                TenderStatus.DRAFTING,
            }:
                tender_row.status = TenderStatus.DRAFTING
            await session.commit()
            payload = serialize_section(row) if row else {"id": section_id, "body": body}
        yield f"data: {json.dumps({'done': True, 'section': payload}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
