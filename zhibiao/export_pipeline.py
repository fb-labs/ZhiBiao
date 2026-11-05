"""按 3001/zhinbiao2 的导出链路出件：封面 → 目录 → 章节（含原件版式）→ 页眉页脚 → PDF。"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path
from typing import Any

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from zhibiao.core.bid_integrity import is_manual_content, repair_generated_text
from zhibiao.core.chapter_rules import container_chapter_numbers, section_numbers
from zhibiao.core.format_sections import (
    build_templates,
    pdf_sections,
    pdf_templates_for_rows,
)
from zhibiao.core.tender_parse import clean_project_name
from zhibiao.export_docx import (
    _AUTOFILL_RULES,
    _GEOM_FIELDS,
    _add_cover,
    _apply_geometry,
    _detach_furniture,
    _fill_toc_page_numbers,
    _is_pdf_page_section,
    _line_spacing_pt,
    _write_body_with_toc,
    _write_page_footer,
    normalize_theme_fonts,
)

logger = logging.getLogger(__name__)

BODY_FONT = "宋体"
BODY_SIZE = 12.0
MARGIN_CM = 2.54
LINE_SPACING_TWIPS = 0
TOC_LEVEL = 3


def _rows_from_sections(sections: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in sections:
        body = str(getattr(section, "body", "") or "").strip()
        if not body:
            continue
        parent = getattr(section, "parent_key", None)
        rows.append(
            {
                "id": str(getattr(section, "id", "") or ""),
                "seq": int(getattr(section, "position", 0) or 0) + 1,
                "level": 2 if parent else 1,
                "title": str(getattr(section, "heading", "") or ""),
                "content": body,
                "words": len(re.sub(r"\s", "", body)),
                "status": "已生成",
                "source": "",
            }
        )
    rows.sort(key=lambda item: item["seq"])
    return rows


def _dims_from_analysis(tender: Any, analysis: Any) -> list[dict[str, Any]]:
    dims: list[dict[str, Any]] = []

    def add(name: str, value: Any) -> None:
        text = str(value or "").strip()
        if name and text and text != "未明确":
            dims.append({"name": name, "value": text, "hit": True})

    add("项目名称", getattr(tender, "title", ""))
    add("项目编号", getattr(tender, "code", ""))
    add("招标人", getattr(tender, "buyer", ""))
    add("采购人", getattr(tender, "buyer", ""))
    add("预算金额", getattr(tender, "budget", ""))
    add("投标截止", getattr(tender, "deadline", ""))
    if analysis:
        for item in getattr(analysis, "dimensions", None) or []:
            if isinstance(item, dict):
                add(str(item.get("label") or ""), item.get("value"))
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in dims:
        key = (item["name"], item["value"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _source_facts(dims: list[dict[str, Any]], tender: Any, source_text: str) -> dict[str, str]:
    facts = {
        str(item.get("name")): str(item.get("value") or "")
        for item in dims
        if item.get("hit") and item.get("value")
    }
    for name, pattern in _AUTOFILL_RULES:
        match = re.search(pattern, source_text, re.M)
        if match and name not in facts:
            facts[name] = match.group(1).strip()
    if "招标人" not in facts and getattr(tender, "buyer", ""):
        facts["招标人"] = str(tender.buyer)
    if "项目编号" not in facts and getattr(tender, "code", ""):
        facts["项目编号"] = str(tender.code)
    if "项目名称" not in facts and getattr(tender, "title", ""):
        facts["项目名称"] = str(tender.title)
    if "招标代理机构" in facts:
        facts["招标代理"] = facts["招标代理机构"]
    return facts


def _setup_document(document: Any) -> None:
    for section in document.sections:
        section.page_width = Cm(21.0)
        section.page_height = Cm(29.7)
        for side in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
            setattr(section, side, Cm(MARGIN_CM))
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(BODY_SIZE)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), BODY_FONT)
    # 与 3001 出件一致：一级 19pt、二级 16、三级 14、四级 12，宋体加粗；
    # 段前 24pt（一级）/10pt（二~四级），段后 0。
    for level, size, before in ((1, 19, 24), (2, 16, 10), (3, 14, 10), (4, 12, 10)):
        style = document.styles[f"Heading {level}"]
        style.font.name = BODY_FONT
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(0)
        srpr = style.element.get_or_add_rPr()
        srf = srpr.find(qn("w:rFonts"))
        if srf is None:
            srf = OxmlElement("w:rFonts")
            srpr.append(srf)
        srf.set(qn("w:eastAsia"), BODY_FONT)


def _apply_line_spacing(document: Any, cover_elements: set[Any]) -> None:
    spacing = _line_spacing_pt(LINE_SPACING_TWIPS)
    if not spacing:
        return
    template_paragraphs = getattr(document, "_zbt_tpl_p", set())
    for paragraph in document.paragraphs:
        if paragraph._p in cover_elements:
            continue
        if id(paragraph._p) in template_paragraphs:
            continue
        xml = paragraph._p.xml
        if 'w:type="page"' in xml and len(xml) < 400:
            continue
        if paragraph.style is not None and str(paragraph.style.name).startswith("Heading"):
            continue
        if paragraph._p.findall(".//" + qn("w:drawing")):
            continue
        paragraph.paragraph_format.line_spacing = Pt(spacing)


def _write_header(document: Any, title: str, code: str) -> None:
    sections = list(document.sections)
    for index, section in enumerate(sections):
        if _is_pdf_page_section(section):
            _detach_furniture(section)
            continue
        if (
            section.header.is_linked_to_previous
            and index > 0
            and _is_pdf_page_section(sections[index - 1])
        ):
            section.header.is_linked_to_previous = False
        paragraph = section.header.paragraphs[0] if section.header.paragraphs else section.header.add_paragraph()
        paragraph.text = f"{title} 投标文件" + (f"（{code}）" if code else "")
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER


def _render(
    *,
    tender: Any,
    sections: list[Any],
    analysis: Any = None,
    source_bytes: bytes | None = None,
    source_name: str = "",
    source_text: str = "",
) -> bytes:
    rows = _rows_from_sections(sections)
    if not rows:
        raise RuntimeError("没有可导出的章节正文")

    dims = _dims_from_analysis(tender, analysis)
    facts = _source_facts(dims, tender, source_text)
    project_title = clean_project_name(str(getattr(tender, "title", "") or "")) or facts.get(
        "项目名称", ""
    )
    project_code = facts.get("项目编号") or facts.get("招标编号") or str(getattr(tender, "code", "") or "")

    for row in rows:
        fixed, notes = repair_generated_text(
            str(row.get("content") or ""),
            str(row.get("title") or ""),
            project_name=project_title,
            manual=is_manual_content(row.get("source")),
        )
        row["content"] = fixed
        for note in notes:
            logger.info("[事实依据修复] %s %s", row["title"], note)

    chapter_numbers = section_numbers(rows)
    containers = container_chapter_numbers(chapter_numbers.values())

    templates: dict[str, bytes] = {}
    pdf_source: bytes | None = None
    pdf_titles: dict[str, Any] = {}
    cover_plan: dict[str, Any] | None = None
    cover_style: dict[str, Any] | None = None
    suffix = Path(source_name).suffix.lower().lstrip(".")
    if source_bytes:
        if suffix in ("docx", "doc", "wps", "rtf"):
            templates = build_templates(source_bytes, source_name, rows)
            if not templates:
                try:
                    from zhibiao.core.cover_source import plan_from_word

                    cover_plan = plan_from_word(source_bytes, source_name)
                except Exception as exc:  # noqa: BLE001
                    logger.info("Word 封面解析失败，使用默认封面：%s", exc)
        elif suffix == "pdf" or source_bytes[:5] == b"%PDF-":
            pdf_source = source_bytes
            try:
                pdf_map = pdf_sections(source_bytes, source_name)
                pdf_titles = pdf_templates_for_rows(pdf_map, rows)
            except Exception as exc:  # noqa: BLE001
                logger.info("PDF 版式解析失败，正文走平台版式：%s", exc)
            try:
                from zhibiao.core.cover_from_pdf import select_pdf_cover

                cover_names = [facts.get("招标人", ""), facts.get("招标代理机构", "")]
                cover_plan = select_pdf_cover(
                    source_bytes, title=project_title, names=[n for n in cover_names if n]
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("PDF 封面解析失败，使用默认封面：%s", exc)

    has_format_outline = bool(templates or pdf_titles) or bool(
        re.search(r"投标文件(格式|组成|编制)|响应文件(格式|组成|编制)", source_text or "")
    )

    document = docx.Document()
    _setup_document(document)

    title = _add_cover(
        document,
        dims=dims,
        project_name=str(getattr(tender, "title", "") or ""),
        scope_title="",
        cover_plan=cover_plan,
        cover_lines=None,
        cover_style=cover_style or {"font": "黑体", "font_size_pt": 14, "title_size": 22},
        profile_facts={},
    )
    cover_elements = {element for element in document.element.body if element.tag != qn("w:sectPr")}
    cover_geometry = {
        name: getattr(document.sections[0], name) for name in _GEOM_FIELDS
    }

    pdf_skip_pages = {
        int(page) for page in ((cover_plan or {}).get("skip_pages") or ())
    }

    # 招标文件给了投标文件格式时，目录只列一级章（与 3001 出件一致）；
    # 平台通用稿才排到三级。
    toc_level = 1 if has_format_outline else TOC_LEVEL

    _write_body_with_toc(
        document,
        rows,
        toc_on=True,
        toc_level=toc_level,
        chinese_top=has_format_outline,
        chapter_numbers=chapter_numbers,
        containers=containers,
        images={},
        templates=templates,
        pdf_source=pdf_source,
        pdf_titles=pdf_titles,
        pdf_skip_pages=pdf_skip_pages,
        source_facts=facts,
        tender_text=source_text,
    )

    _apply_geometry(document.sections[0], cover_geometry)
    if not has_format_outline:
        _write_header(document, title, project_code)
    _write_page_footer(document)
    _apply_line_spacing(document, cover_elements)
    try:
        from zhibiao.core.cover_from_pdf import clear_cover_furniture

        clear_cover_furniture(document)
    except Exception:  # noqa: BLE001
        pass

    buffer = io.BytesIO()
    normalize_theme_fonts(document, BODY_FONT)
    document.save(buffer)
    data = buffer.getvalue()
    try:
        data = _fill_toc_page_numbers(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[导出] 目录页码回填失败：%s", exc)
    return data


async def render_document(
    *,
    tender: Any,
    sections: list[Any],
    analysis: Any = None,
    source_bytes: bytes | None = None,
    source_name: str = "",
    source_text: str = "",
) -> bytes:
    return await asyncio.to_thread(
        _render,
        tender=tender,
        sections=sections,
        analysis=analysis,
        source_bytes=source_bytes,
        source_name=source_name,
        source_text=source_text,
    )


def render_document_sync(
    *,
    tender: Any,
    sections: list[Any],
    analysis: Any = None,
    source_bytes: bytes | None = None,
    source_name: str = "",
    source_text: str = "",
) -> bytes:
    return _render(
        tender=tender,
        sections=sections,
        analysis=analysis,
        source_bytes=source_bytes,
        source_name=source_name,
        source_text=source_text,
    )


def export_to_path(
    path: Path,
    fmt: str,
    *,
    tender: Any,
    sections: list[Any],
    analysis: Any = None,
    source_bytes: bytes | None = None,
    source_name: str = "",
    source_text: str = "",
) -> None:
    """生成 DOCX（可选再转 PDF）并写入目标文件。"""
    from zhibiao.export_docx import convert_bytes_soffice

    docx_bytes = render_document_sync(
        tender=tender,
        sections=sections,
        analysis=analysis,
        source_bytes=source_bytes,
        source_name=source_name,
        source_text=source_text,
    )
    if fmt != "docx":
        docx_bytes = convert_bytes_soffice(docx_bytes, "docx", fmt)
    path.write_bytes(docx_bytes)
