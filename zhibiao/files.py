from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile

from zhibiao.settings import get_settings

MAX_FILE_SIZE = 80 * 1024 * 1024


def _safe_name(name: str) -> str:
    base = Path(name or "upload.bin").name
    return re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", base)[:180] or "upload.bin"


async def save_upload(tender_id: str, upload: UploadFile) -> tuple[Path, int, str]:
    settings = get_settings()
    settings.ensure_dirs()
    target_dir = settings.path(settings.upload_root) / tender_id
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_name(upload.filename or "upload.bin")
    target = target_dir / f"{hashlib.sha256(filename.encode()).hexdigest()[:8]}_{filename}"

    size = 0
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="文件超过 80MB 限制")
            handle.write(chunk)

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return target, size, digest


def extract_text(path: Path, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        raw = path.read_bytes()
        for encoding in ("utf-8", "gb18030", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")

    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)

    if suffix == ".pdf":
        import pdfplumber

        parts = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(part for part in parts if part.strip())

    raise HTTPException(status_code=400, detail="仅支持 PDF、DOCX、TXT、MD、CSV")
