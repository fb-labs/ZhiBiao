from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.capabilities import capability_ready, read_capability
from zhibiao.database import get_db
from zhibiao.domain import FileRole
from zhibiao.files import extract_text, save_upload
from zhibiao.models import Account, AuditEvent, Tender, TenderFile
from zhibiao.ocr import IMAGE_SUFFIXES, ocr_file_text
from zhibiao.security import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenders", tags=["v2-files"])


async def _ocr_fallback(path, filename: str, text: str, parse_state: str) -> tuple[str, str]:
    """图片解析不出文字时按能力开关调用 OCR。

    扫描版 PDF 不在上传时同步识别（整份文件可能几十页，上传请求会长时间挂起）；
    由招标解析 Agent 的 ocr_file 工具在解读阶段按需识别。
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return text, parse_state
    if len((text or "").strip()) >= 30:
        return text, parse_state
    cfg = await read_capability("ocr")
    if not capability_ready(cfg):
        return text, parse_state
    try:
        recognized = await ocr_file_text(path, filename, cfg)
    except Exception as exc:  # noqa: BLE001 - OCR 失败保留原始解析结果
        logger.warning("OCR 识别失败: %s", exc)
        return text, parse_state
    if recognized.strip():
        return recognized, "ocr"
    return text, parse_state


def _serialize(row: TenderFile, include_text: bool = False) -> dict:
    data = {
        "id": row.id,
        "tenderId": row.tender_id,
        "role": row.role,
        "filename": row.filename,
        "size": row.size,
        "mime": row.mime,
        "checksum": row.checksum,
        "parseState": row.parse_state,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }
    if include_text:
        data["text"] = row.text_content or ""
    return data


@router.post("/{tender_id}/files")
async def upload_file(
    tender_id: str,
    file: UploadFile = File(...),
    role: str = Form(default=FileRole.TENDER),
    account: Account = Depends(require_permission("tender.write")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    if role not in FileRole.ALL:
        raise HTTPException(status_code=400, detail="非法文件角色")

    path, size, digest = await save_upload(tender_id, file)
    text = ""
    parse_state = "parsed"
    try:
        text = await asyncio.to_thread(extract_text, path, file.filename or path.name)
    except HTTPException:
        parse_state = "unsupported"
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析文件失败: %s", exc)
        parse_state = "failed"

    text, parse_state = await _ocr_fallback(
        path, file.filename or path.name, text, parse_state
    )

    record = TenderFile(
        tender_id=tender_id,
        role=role,
        filename=file.filename or path.name,
        stored_path=str(path),
        size=size,
        mime=file.content_type or "",
        checksum=digest,
        text_content=text or None,
        parse_state=parse_state,
    )
    db.add(record)
    db.add(
        AuditEvent(
            actor=account.username,
            action="tender.file.upload",
            target_type="tender",
            target_id=tender_id,
            detail={"filename": record.filename, "role": role, "size": size},
        )
    )
    await db.flush()
    return _serialize(record)


@router.get("/{tender_id}/files")
async def list_files(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(TenderFile)
            .where(TenderFile.tender_id == tender_id)
            .order_by(TenderFile.created_at.desc())
        )
    ).scalars().all()
    return {"items": [_serialize(row) for row in rows]}


@router.get("/{tender_id}/files/{file_id}/text")
async def file_text(
    tender_id: str,
    file_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(TenderFile, file_id)
    if not row or row.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    return _serialize(row, include_text=True)


@router.get("/{tender_id}/files/{file_id}/download")
async def download_file(
    tender_id: str,
    file_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(TenderFile, file_id)
    if not row or row.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(row.stored_path, filename=row.filename)


@router.delete("/{tender_id}/files/{file_id}")
async def delete_file(
    tender_id: str,
    file_id: str,
    account: Account = Depends(require_permission("tender.write")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(TenderFile, file_id)
    if not row or row.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    await db.delete(row)
    db.add(
        AuditEvent(
            actor=account.username,
            action="tender.file.delete",
            target_type="tender",
            target_id=tender_id,
            detail={"filename": row.filename},
        )
    )
    return {"ok": True}
