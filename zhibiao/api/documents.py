from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.api.analysis import current_analysis, serialize as serialize_analysis
from zhibiao.database import get_db, session_factory
from zhibiao.domain import TenderStatus
from zhibiao.llm import get_chat_client
from zhibiao.models import Account, BidDocument, BidSection, Tender
from zhibiao.prompts import write_messages
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-documents"])


class SectionPatch(BaseModel):
    heading: str | None = Field(default=None, min_length=1, max_length=255)
    intent: str | None = None
    body: str | None = None


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
    await db.flush()
    return serialize_section(row)


def _neighbour_context(sections: list[BidSection], current: BidSection, limit: int = 3) -> str:
    written = [row for row in sections if row.position < current.position and (row.body or "").strip()]
    blocks = []
    for row in written[-limit:]:
        blocks.append(f"{row.heading}：{(row.body or '').strip()[:1200]}")
    return "\n".join(blocks)


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
    brief = serialize_analysis(analysis) if analysis else None
    analysis_brief = "（尚未执行招标解读）"
    if brief:
        analysis_brief = brief["overview"]
        if brief["requirements"]:
            analysis_brief += "\n实质性要求：" + "；".join(brief["requirements"][:12])

    messages = write_messages(
        tender.title,
        section.heading,
        section.intent,
        section.target_words,
        _neighbour_context(list(sections), section),
        analysis_brief,
    )

    async def event_stream() -> AsyncIterator[str]:
        chunks: list[str] = []
        try:
            client = await get_chat_client()
            async for delta in client.stream(
                messages, max_tokens=max(1200, min(6000, section.target_words * 2))
            ):
                chunks.append(delta)
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("章节生成失败: %s", exc)
            yield f"data: {json.dumps({'error': f'模型调用失败: {exc}'}, ensure_ascii=False)}\n\n"
            return

        body = "".join(chunks).strip()
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
