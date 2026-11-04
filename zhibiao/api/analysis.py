from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.corpus import tender_corpus
from zhibiao.database import get_db
from zhibiao.domain import Severity, TenderStatus
from zhibiao.llm import get_chat_client
from zhibiao.models import Account, Analysis, AuditEvent, Tender
from zhibiao.prompts import analysis_messages
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-analysis"])

SEVERITIES = {Severity.BLOCKER, Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.INFO}


def serialize(row: Analysis, include_requirements: bool = True) -> dict:
    data = {
        "id": row.id,
        "tenderId": row.tender_id,
        "revision": row.revision,
        "model": row.model,
        "overview": row.overview,
        "dimensions": row.dimensions or [],
        "scoring": row.scoring or {},
        "risks": row.risks or [],
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }
    if include_requirements:
        data["requirements"] = getattr(row, "requirements", None) or []
    return data


def _clean_dimensions(raw) -> list[dict]:
    items: list[dict] = []
    if not isinstance(raw, list):
        return items
    for entry in raw[:12]:
        if not isinstance(entry, dict):
            continue
        items.append(
            {
                "key": str(entry.get("key") or ""),
                "label": str(entry.get("label") or "").strip(),
                "value": str(entry.get("value") or "").strip(),
                "note": str(entry.get("note") or "").strip(),
            }
        )
    return [item for item in items if item["label"] or item["value"]]


def _clean_scoring(raw) -> dict:
    if not isinstance(raw, dict):
        return {"method": "", "total": 0, "items": []}
    items: list[dict] = []
    for entry in (raw.get("items") or [])[:30]:
        if not isinstance(entry, dict):
            continue
        try:
            points = float(entry.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        items.append(
            {
                "name": str(entry.get("name") or "").strip(),
                "points": points,
                "criteria": str(entry.get("criteria") or "").strip(),
            }
        )
    try:
        total = float(raw.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return {"method": str(raw.get("method") or "").strip(), "total": total, "items": items}


def _clean_risks(raw) -> list[dict]:
    risks: list[dict] = []
    if not isinstance(raw, list):
        return risks
    for entry in raw[:20]:
        if not isinstance(entry, dict):
            continue
        level = str(entry.get("level") or Severity.MINOR).lower()
        if level not in SEVERITIES:
            level = Severity.MINOR
        risks.append(
            {
                "level": level,
                "title": str(entry.get("title") or "").strip(),
                "detail": str(entry.get("detail") or "").strip(),
            }
        )
    return risks


def _clean_requirements(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(entry).strip()[:300] for entry in raw[:30] if str(entry).strip()]


def _autofill(tender: Tender, dimensions: list[dict]) -> None:
    for item in dimensions:
        label = item.get("label", "")
        value = item.get("value", "")
        if not value or value == "未明确":
            continue
        if not tender.budget and "预算" in label:
            tender.budget = value[:80]
        if not tender.deadline and "截止" in label:
            tender.deadline = value[:40]


async def current_analysis(db: AsyncSession, tender_id: str) -> Analysis | None:
    return (
        await db.execute(
            select(Analysis)
            .where(Analysis.tender_id == tender_id, Analysis.is_current.is_(True))
            .order_by(Analysis.created_at.desc())
        )
    ).scalar_one_or_none()


@router.post("/{tender_id}/analysis")
async def run_analysis(
    tender_id: str,
    account: Account = Depends(require_permission("analysis.run")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    corpus = await tender_corpus(db, tender)
    if len(corpus) < 200:
        raise HTTPException(status_code=400, detail="请先上传可解析的招标文件，正文过短")

    client = await get_chat_client()
    try:
        payload = await client.json_chat(
            analysis_messages(tender.title, tender.buyer, corpus), max_tokens=6000
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("招标解读失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"模型调用失败: {exc}") from exc

    dimensions = _clean_dimensions(payload.get("dimensions"))
    if not dimensions:
        raise HTTPException(status_code=502, detail="模型未返回有效的解读维度")

    await db.execute(
        update(Analysis)
        .where(Analysis.tender_id == tender_id, Analysis.is_current.is_(True))
        .values(is_current=False)
    )
    latest = await db.scalar(
        select(Analysis.revision).where(Analysis.tender_id == tender_id).order_by(
            Analysis.revision.desc()
        )
    )
    analysis = Analysis(
        tender_id=tender_id,
        revision=(latest or 0) + 1,
        model=client.model,
        overview=str(payload.get("overview") or "").strip(),
        dimensions=dimensions,
        scoring=_clean_scoring(payload.get("scoring")),
        risks=_clean_risks(payload.get("risks")),
        requirements=_clean_requirements(payload.get("requirements")),
        is_current=True,
    )
    db.add(analysis)
    _autofill(tender, dimensions)
    if tender.status in {TenderStatus.INTAKE, TenderStatus.ANALYZED}:
        tender.status = TenderStatus.ANALYZED
    db.add(
        AuditEvent(
            actor=account.username,
            action="analysis.run",
            target_type="tender",
            target_id=tender_id,
            detail={"revision": analysis.revision, "model": client.model},
        )
    )
    await db.flush()
    return serialize(analysis)


@router.get("/{tender_id}/analysis")
async def get_analysis(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    row = await current_analysis(db, tender_id)
    if not row:
        raise HTTPException(status_code=404, detail="尚未执行招标解读")
    return serialize(row)
