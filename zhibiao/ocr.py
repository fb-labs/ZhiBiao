"""OCR 接口：调用兼容 OpenAI 协议的视觉模型识别扫描件文字。

仅在「模型服务 → 能力开关 → OCR 接口」开启并配置了接口地址时才会被调用；
关闭时上传解析与其它流程完全保持原样。
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_MAX_IMAGE_BYTES = 12 * 1024 * 1024

OCR_PROMPT = (
    "你是投标文件 OCR 引擎。请逐页识别图片中的全部文字，保持原有段落与表格顺序，"
    "数字、编号、金额、日期必须逐字准确。只输出识别到的文字，不要解释、不要总结、"
    "不要补充图片里没有的内容。"
)


def is_image_file(filename: str | None) -> bool:
    return Path(str(filename or "")).suffix.lower() in IMAGE_SUFFIXES


def _mime_of(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    if suffix in mapping:
        return mapping[suffix]
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


def _data_url(data: bytes, mime: str) -> str:
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def _content_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts).strip()
    return ""


async def _chat(cfg: dict, content: list[dict]) -> str:
    base_url = str(cfg.get("baseUrl") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("OCR 未配置接口地址")
    model = str(cfg.get("model") or "").strip() or "glm-4.5v"
    api_key = str(cfg.get("apiKey") or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 4096,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{base_url}/chat/completions", json=body, headers=headers)
        response.raise_for_status()
        return _content_text(response.json())


async def ocr_image(data: bytes, mime: str, cfg: dict) -> str:
    """识别一张图片，返回纯文本。"""
    if len(data) > _MAX_IMAGE_BYTES:
        raise RuntimeError("图片超过 12MB，无法通过 OCR 接口识别")
    content = [
        {"type": "text", "text": OCR_PROMPT},
        {"type": "image_url", "image_url": {"url": _data_url(data, mime)}},
    ]
    return await _chat(cfg, content)


def render_pdf_pages(path: Path, max_pages: int = 10) -> list[bytes]:
    """把 PDF 前 N 页渲染成 PNG（扫描件 OCR 用），同步执行，调用方放线程里跑。"""
    import pdfplumber

    pages: list[bytes] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[: max(1, int(max_pages))]:
            image = page.to_image(resolution=144).original
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pages.append(buffer.getvalue())
    return pages


async def ocr_file_text(path: Path, filename: str, cfg: dict) -> str:
    """识别上传文件：图片直接识别，PDF 渲染前 N 页后逐页识别。"""
    suffix = Path(filename or path.name).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        data = path.read_bytes()
        return await ocr_image(data, _mime_of(filename, data), cfg)
    if suffix == ".pdf":
        import asyncio

        pages = await asyncio.to_thread(
            render_pdf_pages, path, int(cfg.get("maxPages") or 10)
        )
        texts: list[str] = []
        for page in pages:
            text = await ocr_image(page, "image/png", cfg)
            if text:
                texts.append(text)
        return "\n\n".join(texts)
    raise RuntimeError("OCR 仅支持图片与 PDF")
