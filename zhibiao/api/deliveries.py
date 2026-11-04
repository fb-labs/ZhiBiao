from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.api.documents import document_of
from zhibiao.database import get_db
from zhibiao.domain import DeliveryFormat, TenderStatus
from zhibiao.models import Account, AuditEvent, BidSection, Delivery, Tender
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-delivery"])
detail_router = APIRouter(prefix="/deliveries", tags=["v2-delivery"])


class DeliveryCreate(BaseModel):
    format: str = Field(default=DeliveryFormat.DOCX)


def serialize(row: Delivery) -> dict:
    path = Path(row.file_path) if row.file_path else None
    size = path.stat().st_size if path and path.is_file() else 0
    return {
        "id": row.id,
        "tenderId": row.tender_id,
        "documentId": row.document_id,
        "format": row.file_format,
        "filename": path.name if path else "",
        "size": size,
        "status": row.status,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "downloadUrl": f"/api/deliveries/{row.id}/download",
    }


def _markdown_to_lines(body: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for raw in (body or "").splitlines():
        text = raw.strip()
        if not text:
            continue
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`(.+?)`", r"\1", text)
        if text.startswith("### "):
            lines.append(("h3", text[4:].strip()))
        elif text.startswith("## "):
            lines.append(("h2", text[3:].strip()))
        elif text.startswith("# "):
            lines.append(("h1", text[2:].strip()))
        elif re.match(r"^[-*] ", text):
            lines.append(("item", text[2:].strip()))
        elif re.match(r"^\d+[.、)）] ", text):
            lines.append(("item", text))
        else:
            lines.append(("text", text))
    return lines


def _write_docx(path: Path, tender: Tender, sections: list[BidSection]) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(tender.title)
    run.bold = True
    run.font.size = Pt(22)

    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta = " · ".join(part for part in (tender.buyer, tender.code) if part)
    subtitle.add_run(meta).font.size = Pt(12)

    for section in sections:
        heading = re.sub(r"^#+\s*", "", section.heading).strip()
        document.add_heading(heading, level=1 if not section.parent_key else 2)
        for kind, text in _markdown_to_lines(section.body):
            if kind == "h1":
                document.add_heading(text, level=2)
            elif kind == "h2":
                document.add_heading(text, level=3)
            elif kind == "h3":
                document.add_heading(text, level=4)
            elif kind == "item":
                document.add_paragraph(text, style="List Bullet")
            else:
                document.add_paragraph(text)
    document.save(str(path))


def _pick_font() -> str | None:
    candidates = [
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simsun.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def _write_pdf(path: Path, tender: Tender, sections: list[BidSection]) -> None:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise HTTPException(status_code=400, detail="服务器未安装 reportlab，无法生成 PDF") from exc

    font_path = _pick_font()
    if not font_path:
        raise HTTPException(status_code=400, detail="服务器缺少中文字体，请改用 DOCX 交付")
    font_name = "ZhiBiaoCJK"
    kwargs = {}
    if font_path.lower().endswith(".ttc"):
        kwargs["subfontIndex"] = 0
    pdfmetrics.registerFont(TTFont(font_name, font_path, **kwargs))

    styles = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=styles["Normal"], fontName=font_name, fontSize=10.5, leading=18)
    h1 = ParagraphStyle("h1", parent=base, fontSize=18, leading=28, spaceAfter=10, alignment=1)
    h2 = ParagraphStyle("h2", parent=base, fontSize=14, leading=24, spaceBefore=12, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=base, fontSize=12, leading=22, spaceBefore=8, spaceAfter=4)
    item = ParagraphStyle("item", parent=base, leftIndent=14, bulletIndent=4)

    flow = [Paragraph(tender.title, h1), Paragraph(tender.buyer or tender.code, base), Spacer(1, 8)]
    for section in sections:
        flow.append(Paragraph(re.sub(r"^#+\s*", "", section.heading), h2))
        for kind, text in _markdown_to_lines(section.body):
            escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if kind in {"h1", "h2"}:
                flow.append(Paragraph(escaped, h3))
            elif kind == "item":
                flow.append(Paragraph(f"• {escaped}", item))
            else:
                flow.append(Paragraph(escaped, base))

    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=tender.title,
    )
    document.build(flow)


@router.post("/{tender_id}/deliveries")
async def create_delivery(
    tender_id: str,
    payload: DeliveryCreate,
    account: Account = Depends(require_permission("delivery.run")),
    db: AsyncSession = Depends(get_db),
):
    if payload.format not in {DeliveryFormat.DOCX, DeliveryFormat.PDF}:
        raise HTTPException(status_code=400, detail="仅支持 DOCX 或 PDF")
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    document = await document_of(db, tender_id)
    if not document:
        raise HTTPException(status_code=400, detail="请先生成标书大纲")
    sections = (
        await db.execute(
            select(BidSection)
            .where(BidSection.document_id == document.id)
            .order_by(BidSection.position.asc())
        )
    ).scalars().all()
    if not sections or not any((row.body or "").strip() for row in sections):
        raise HTTPException(status_code=400, detail="标书正文为空，无法生成交付文件")

    from zhibiao.settings import get_settings

    settings = get_settings()
    settings.ensure_dirs()
    target_dir = settings.path(settings.delivery_root) / tender_id
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    suffix = "docx" if payload.format == DeliveryFormat.DOCX else "pdf"
    target = target_dir / f"{tender.code}-{stamp}.{suffix}"

    try:
        if payload.format == DeliveryFormat.DOCX:
            _write_docx(target, tender, list(sections))
        else:
            _write_pdf(target, tender, list(sections))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("交付文件生成失败: %s", exc)
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"生成交付文件失败: {exc}") from exc

    row = Delivery(
        tender_id=tender_id,
        document_id=document.id,
        file_format=payload.format,
        file_path=str(target),
        status="ready",
    )
    db.add(row)
    tender.status = TenderStatus.COMPLETE
    db.add(
        AuditEvent(
            actor=account.username,
            action="delivery.create",
            target_type="tender",
            target_id=tender_id,
            detail={"format": payload.format, "file": target.name},
        )
    )
    await db.flush()
    return serialize(row)


@router.get("/{tender_id}/deliveries")
async def list_deliveries(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Delivery)
            .where(Delivery.tender_id == tender_id)
            .order_by(Delivery.created_at.desc())
        )
    ).scalars().all()
    return {"items": [serialize(row) for row in rows]}


@detail_router.get("/{delivery_id}/download")
async def download_delivery(
    delivery_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Delivery, delivery_id)
    if not row or not row.file_path or not Path(row.file_path).is_file():
        raise HTTPException(status_code=404, detail="交付文件不存在")
    filename = Path(row.file_path).name
    return FileResponse(row.file_path, filename=filename)
