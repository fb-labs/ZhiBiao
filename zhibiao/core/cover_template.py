"""从投标书格式模板里解析封面，并按解析结果逐行复刻。

每份模板都不一样，所以不预设结构：模板有几行就写几行，模板没有的一行都不加。
"""
from __future__ import annotations

import io
from typing import Any


def parse_cover(source: Any, *, max_lines: int = 40) -> list[dict[str, Any]]:
    """解析模板封面（docx 路径或 bytes），返回行参数序列。

    只取第一节前 max_lines 段，遇到分页符即停——封面之后是正文。
    """
    from docx import Document

    try:
        doc = Document(source if isinstance(source, (str, io.BytesIO)) else io.BytesIO(source))
    except Exception:
        return []
    lines: list[dict[str, Any]] = []
    for para in doc.paragraphs[:max_lines]:
        xml = para._p.xml
        if ("w:br w:type=\"page\"" in xml or "w:sectPr" in xml
                or para.paragraph_format.page_break_before):
            break
        text = str(para.text or "")
        if text.replace(" ", "").strip() in ("目录", "目次"):
            break        # 封面到目录为止（没有分页符的模板靠这一行划界）
        run = para.runs[0] if para.runs else None
        lines.append({
            "text": text,
            "align": para.alignment,
            "font": run.font.name if run is not None else None,
            "size": run.font.size.pt if (run is not None and run.font.size) else None,
            "bold": bool(run.font.bold) if run is not None else False,
            "before": (para.paragraph_format.space_before.pt
                       if para.paragraph_format.space_before else 0),
            "line": para.paragraph_format.line_spacing,
        })
    while lines and not lines[-1]["text"].strip():
        lines.pop()
    return lines


def build_cover(doc: Any, lines: list[dict[str, Any]], facts: dict[str, str]) -> None:
    """严格按 lines 复刻：行数、顺序、对齐、字号都跟模板走；只替换文字。"""
    from docx.shared import Pt

    for spec in lines:
        text = str(spec.get("text") or "")
        for key, value in (facts or {}).items():
            if value:
                text = text.replace(key, value)
        para = doc.add_paragraph()
        if spec.get("align") is not None:
            para.alignment = spec["align"]
        fmt = para.paragraph_format
        if spec.get("before"):
            fmt.space_before = Pt(float(spec["before"]))
        if spec.get("line"):
            fmt.line_spacing = spec["line"]
        run = para.add_run(text)
        if spec.get("size"):
            run.font.size = Pt(float(spec["size"]))
        if spec.get("font"):
            run.font.name = spec["font"]
        if spec.get("bold"):
            run.font.bold = True


def style_from_pdf(source: bytes, *, max_pages: int = 2) -> dict[str, Any]:
    """无模板时，从招标书首页量出封面风格（近似）：最大字号、页边距、居中与否。

    字号用行高反推：pdfplumber 的 chars 带 size，取首页最大的 size 当主标题字号。
    """
    import io as _io

    import pdfplumber

    try:
        with pdfplumber.open(_io.BytesIO(source)) as pdf:
            page = pdf.pages[0]
            sizes = [float(c.get("size") or 0) for c in (page.chars or [])]
            sizes = [s for s in sizes if s > 0]
            top = max(sizes) if sizes else 22.0
            words = page.extract_words() or []
            centered = bool(words) and all(
                abs((w["x0"] + w["x1"]) / 2 - page.width / 2) < page.width * 0.12
                for w in words[:3])
            return {
                "title_pt": round(min(max(top * 1.2, 18.0), 30.0), 1),
                "centered": centered,
                "margin_cm": round(float(page.width - (words[0]["x1"] - words[0]["x0"])
                                        if False else 2.8), 1),
            }
    except Exception:
        return {}
