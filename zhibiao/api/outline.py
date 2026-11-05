from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.api.analysis import current_analysis, serialize as serialize_analysis
from zhibiao.corpus import tender_corpus
from zhibiao.database import get_db
from zhibiao.domain import TenderStatus
from zhibiao.generation.outline import (
    SectionPlan,
    assign_targets,
    build_outline as build_plan,
    expand_template_scores,
    match_scores,
    tender_plan,
)
from zhibiao.llm import get_chat_client
from zhibiao.models import Account, AuditEvent, BidDocument, BidSection, Outline, Tender
from zhibiao.prompts import outline_messages
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-outline"])


class OutlineOptions(BaseModel):
    mode: str = Field(default="score")
    targetWords: int = Field(default=80000, ge=10000, le=300000)
    templateId: str | None = None


async def _document_of(db: AsyncSession, tender_id: str) -> BidDocument | None:
    return (
        await db.execute(
            select(BidDocument)
            .where(BidDocument.tender_id == tender_id)
            .order_by(BidDocument.created_at.desc())
        )
    ).scalar_one_or_none()


def serialize_outline(outline: Outline | None, document: BidDocument | None, sections: list[BidSection]) -> dict:
    meta = (outline.sections if outline and isinstance(outline.sections, dict) else {}) or {}
    return {
        "id": outline.id if outline else None,
        "tenderId": outline.tender_id if outline else (document.tender_id if document else ""),
        "revision": outline.revision if outline else 0,
        "documentId": document.id if document else None,
        "mode": meta.get("mode"),
        "note": meta.get("note"),
        "targetWords": meta.get("targetWords"),
        "sections": [
            {
                "id": row.id,
                "position": row.position,
                "parentKey": row.parent_key,
                "heading": row.heading,
                "intent": row.intent,
                "targetWords": row.target_words,
                "body": row.body,
                "status": row.status,
            }
            for row in sections
        ],
        "createdAt": outline.created_at.isoformat() if outline and outline.created_at else None,
    }


def _clean_llm_sections(raw) -> list[dict]:
    items = raw if isinstance(raw, list) else []
    cleaned: list[dict] = []
    for entry in items[:12]:
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("heading") or "").strip()
        if not heading:
            continue
        children = []
        for child in (entry.get("children") or [])[:8]:
            if not isinstance(child, dict):
                continue
            title = str(child.get("heading") or "").strip()
            if not title:
                continue
            children.append(
                {
                    "heading": title[:255],
                    "intent": str(child.get("intent") or "").strip(),
                }
            )
        cleaned.append(
            {
                "heading": heading[:255],
                "intent": str(entry.get("intent") or "").strip(),
                "children": children,
            }
        )
    return cleaned


def _plans_from_llm(raw) -> list[SectionPlan]:
    plans: list[SectionPlan] = []
    for top in _clean_llm_sections(raw):
        plans.append(
            SectionPlan(heading=top["heading"], intent=top["intent"], level=1, weight=1.0)
        )
        for child in top["children"]:
            plans.append(
                SectionPlan(heading=child["heading"], intent=child["intent"], level=2, weight=1.0)
            )
    return plans


async def _load_current(
    db: AsyncSession, tender_id: str
) -> tuple[Outline | None, BidDocument | None, list[BidSection]]:
    outline = await db.scalar(
        select(Outline)
        .where(Outline.tender_id == tender_id, Outline.is_current.is_(True))
        .order_by(Outline.created_at.desc())
        .limit(1)
    )
    document = await _document_of(db, tender_id)
    sections: list[BidSection] = []
    if document:
        sections = list(
            (
                await db.execute(
                    select(BidSection)
                    .where(BidSection.document_id == document.id)
                    .order_by(BidSection.position.asc())
                )
            ).scalars().all()
        )
    return outline, document, sections


async def build_outline_inline(
    db: AsyncSession,
    tender: Tender,
    options: OutlineOptions,
    actor: str,
) -> dict:
    """大纲的确定实现：模板识别 → 章节规划 → 评分对齐 → 落库。

    规划 Agent 的 save_outline 工具与端点兜底都调用这里。
    """
    analysis = await current_analysis(db, tender.id)
    mode = options.mode if options.mode in {"score", "standard", "tender"} else "score"
    if mode == "score" and not analysis:
        raise HTTPException(status_code=400, detail="请先完成招标解读")

    # 招标文件里只要写了「投标文件格式 / 投标文件组成及格式」，目录就必须按它来：
    # 顶层章名、顺序、数量完全照抄，不再让模型另起一套 25 章的结构。
    corpus = await tender_corpus(db, tender, limit=160000)
    # 只有真正从原件格式章抽到目录才算「有模板」：build_plan 抽不到时会回退成
    # 28 章标准结构，旧代码用 len >= 3 判断，于是把标准 IT 结构也当成原件目录，
    # 提示语写着「已按原件目录生成」，实际整份大纲都是通用章节。
    template_plans = tender_plan(corpus)
    has_template = len(template_plans) >= 3
    if has_template and mode in {"score", "standard"}:
        mode = "tender"
        plans = template_plans
        note = f"检测到招标文件投标文件格式章，已按原件目录生成（{sum(1 for p in plans if p.level == 1)} 个一级章）"
    elif mode in {"standard", "tender"}:
        plans, note = build_plan(mode, corpus)
        if options.templateId:
            note += "（未使用指定模板，当前按内置结构生成）"
    else:
        client = await get_chat_client(assistant="outliner")
        try:
            result = await client.json_chat(
                outline_messages(tender.title, serialize_analysis(analysis), options.targetWords),
                max_tokens=6000,
            )
            plans = _plans_from_llm(result.get("sections"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("大纲生成失败: %s", exc)
            raise HTTPException(status_code=502, detail=f"模型调用失败: {exc}") from exc
        if not plans:
            raise HTTPException(status_code=502, detail="模型未返回有效章节结构")
        note = "按评分项对齐生成，覆盖评分办法与实质性要求"

    score_items = ((analysis.scoring or {}).get("items") or []) if analysis else []
    if mode == "tender":
        plans = expand_template_scores(plans, score_items)
    match_scores(plans, score_items)
    assign_targets(plans, options.targetWords)
    if not plans:
        raise HTTPException(status_code=502, detail="未能生成章节结构")

    document = await _document_of(db, tender.id)
    if not document:
        document = BidDocument(tender_id=tender.id, title=f"{tender.title} 投标文件", status="draft")
        db.add(document)
        await db.flush()
    else:
        await db.execute(delete(BidSection).where(BidSection.document_id == document.id))

    position = 0
    parent_key: str | None = None
    section_meta: list[dict] = []
    for index, plan in enumerate(plans, start=1):
        if plan.level == 1:
            parent_key = f"s{index}"
        row_parent = None if plan.level == 1 else parent_key
        db.add(
            BidSection(
                document_id=document.id,
                position=position,
                parent_key=row_parent,
                heading=plan.heading,
                intent=plan.intent,
                target_words=plan.target_words,
            )
        )
        section_meta.append(
            {
                **plan.as_dict(),
                "parentKey": row_parent,
            }
        )
        position += 1

    await db.execute(
        update(Outline)
        .where(Outline.tender_id == tender.id, Outline.is_current.is_(True))
        .values(is_current=False)
    )
    latest = await db.scalar(
        select(Outline.revision)
        .where(Outline.tender_id == tender.id)
        .order_by(Outline.revision.desc())
    )
    outline = Outline(
        tender_id=tender.id,
        revision=(latest or 0) + 1,
        sections={
            "mode": mode,
            "note": note,
            "targetWords": options.targetWords,
            "plans": section_meta,
        },
        is_current=True,
    )
    db.add(outline)
    if tender.status in {TenderStatus.INTAKE, TenderStatus.ANALYZED, TenderStatus.OUTLINED}:
        tender.status = TenderStatus.OUTLINED
    db.add(
        AuditEvent(
            actor=actor,
            action="outline.build",
            target_type="tender",
            target_id=tender.id,
            detail={
                "revision": outline.revision,
                "mode": mode,
                "sections": position,
                "targetWords": options.targetWords,
            },
        )
    )
    await db.flush()

    sections = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    return serialize_outline(outline, document, list(sections))


@router.post("/{tender_id}/outline")
async def build_outline(
    tender_id: str,
    payload: OutlineOptions | None = None,
    account: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    options = payload or OutlineOptions()
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    analysis = await current_analysis(db, tender_id)
    if options.mode == "score" and not analysis:
        raise HTTPException(status_code=400, detail="请先完成招标解读")

    # 这一步的执行者是生成规划 Agent：识模板 → 选模式 → 生成大纲。
    from zhibiao.agents.steps import run_step_agent

    actor = account.username
    await run_step_agent(
        "planner",
        tender_id=tender_id,
        task="判断生成模式、抽取模板章节并生成可写作的大纲，然后保存到系统。",
        payload={
            "mode": options.mode,
            "targetWords": options.targetWords,
            "templateId": options.templateId,
        },
        account=actor,
    )
    db.expire_all()
    outline, document, sections = await _load_current(db, tender_id)
    if not document or not sections:
        return await build_outline_inline(db, await db.get(Tender, tender_id), options, actor)
    return serialize_outline(outline, document, sections)


@router.get("/{tender_id}/outline")
async def get_outline(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    document = await _document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=404, detail="尚未生成标书大纲")
    outline = (
        await db.execute(
            select(Outline)
            .where(Outline.tender_id == tender_id, Outline.is_current.is_(True))
            .order_by(Outline.created_at.desc())
        )
    ).scalar_one_or_none()
    sections = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    return serialize_outline(outline, document, list(sections))
