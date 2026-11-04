from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.domain import FileRole
from zhibiao.models import Analysis, BidDocument, BidSection, Tender, TenderFile


async def tender_corpus(db: AsyncSession, tender: Tender, limit: int = 60000) -> str:
    rows = (
        await db.execute(
            select(TenderFile)
            .where(TenderFile.tender_id == tender.id, TenderFile.text_content.is_not(None))
            .order_by(TenderFile.created_at.asc())
        )
    ).scalars().all()
    ordered = [row for row in rows if row.role == FileRole.TENDER] + [
        row for row in rows if row.role != FileRole.TENDER
    ]
    parts: list[str] = []
    if tender.summary.strip():
        parts.append(f"【项目摘要】\n{tender.summary.strip()}")
    for row in ordered:
        text = (row.text_content or "").strip()
        if not text:
            continue
        parts.append(f"【{row.filename}】\n{text}")
    merged = "\n\n".join(parts).strip()
    return merged[:limit]


def analysis_brief(analysis: Analysis | None, limit: int = 3000) -> str:
    if not analysis:
        return "（尚未执行招标解读）"
    lines = [analysis.overview or ""]
    for item in analysis.dimensions or []:
        if isinstance(item, dict):
            lines.append(f"{item.get('label', '')}：{item.get('value', '')}")
    scoring = analysis.scoring or {}
    if isinstance(scoring, dict) and scoring.get("items"):
        summary = "；".join(
            f"{entry.get('name', '')}({entry.get('points', '')}分)"
            for entry in scoring["items"]
            if isinstance(entry, dict)
        )
        lines.append(f"评分项：{summary}")
    requirements = analysis.requirements if hasattr(analysis, "requirements") else []
    if isinstance(requirements, list) and requirements:
        lines.append("实质性要求：" + "；".join(str(entry) for entry in requirements))
    return "\n".join(line for line in lines if line).strip()[:limit]


async def document_sections(
    db: AsyncSession, document: BidDocument | None
) -> list[BidSection]:
    if not document:
        return []
    rows = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    return list(rows)


def document_text(sections: list[BidSection], limit: int = 60000) -> str:
    blocks: list[str] = []
    for section in sections:
        body = (section.body or "").strip()
        blocks.append(f"## {section.heading}\n{body}")
    return "\n\n".join(blocks)[:limit]
