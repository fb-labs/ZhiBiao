from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from zhibiao.core.document_structure import normalize_export_structure
from zhibiao.text_hygiene import fix_quote_pairing, hygienic_text

_MD_TABLE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_MD_SEPARATOR = re.compile(r"^[ \t]*\|[ \t:|-]+\|[ \t]*$")
_MD_HEAD = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_MD_UL = re.compile(r"^[ \t]*[-*][ \t]+(.+)$")
_MD_OL = re.compile(r"^[ \t]*(\d+[.、)）])[ \t]+(.+)$")
_MD_HR = re.compile(r"^[ \t]*([-*_][ \t]*){3,}$")
_MD_IMG = re.compile(r"^[ \t]*!\[([^\]\n]*)\]\(([^)\n]+)\)[ \t]*$")
_BOLD = re.compile(r"(\*\*[^*]+\*\*)")


def _set_east_asian(style: Any, font_name: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    style.font.name = font_name
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), font_name)


def _setup_styles(doc: Any) -> None:
    from docx.shared import Pt, RGBColor

    normal = doc.styles["Normal"]
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.5
    _set_east_asian(normal, "宋体")

    for level, size in ((1, 18), (2, 15), (3, 13.5), (4, 12)):
        style = doc.styles[f"Heading {level}"]
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        _set_east_asian(style, "宋体")


def _setup_page(section: Any, cover: bool = False) -> None:
    from docx.shared import Cm

    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)
    section.header_distance = Cm(1.5)
    section.footer_distance = Cm(1.6)


def _add_field(paragraph: Any, instruction: str, placeholder: str = "1") -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    run._r.append(begin)

    run = paragraph.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" {instruction} "
    run._r.append(instr)

    run = paragraph.add_run()
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    run._r.append(separate)

    paragraph.add_run(placeholder)

    run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(end)


def _set_page_number_start(section: Any, start: int = 1) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    sect_pr = section._sectPr
    pg_type = sect_pr.find(qn("w:pgNumType"))
    if pg_type is None:
        pg_type = OxmlElement("w:pgNumType")
        sect_pr.append(pg_type)
    pg_type.set(qn("w:start"), str(start))


def _add_runs(paragraph: Any, text: str) -> None:
    for part in _BOLD.split(str(text or "")):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        else:
            paragraph.add_run(part.replace("`", ""))


def _section_numbering(sections: list[Any]) -> list[dict]:
    plan: list[dict] = []
    children = {row.parent_key for row in sections if row.parent_key}
    top_number = 0
    current_top = 0
    current_parent = None
    child_number = 0
    for section_index, section in enumerate(sections, start=1):
        if not section.parent_key:
            top_number += 1
            current_top = top_number
            current_parent = f"s{section_index}"
            child_number = 0
            number = str(top_number)
            level = 1
            container = current_parent in children
        else:
            if current_parent != section.parent_key:
                child_number = 0
                current_parent = section.parent_key
            child_number += 1
            number = f"{current_top}.{child_number}"
            level = 2
            container = False
        body, _ = hygienic_text(section.body or "")
        body = fix_quote_pairing(body)
        normalized = normalize_export_structure(
            body,
            section.heading or "",
            number,
            container=container,
        )
        plan.append(
            {
                "section": section,
                "number": number,
                "level": level,
                "container": container,
                "body": normalized,
            }
        )
    return plan


def _body_headings(plan_row: dict) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    base = int(plan_row["level"])
    for line in str(plan_row["body"] or "").splitlines():
        matched = _MD_HEAD.match(line.strip())
        if not matched:
            continue
        depth = len(matched.group(1))
        text = matched.group(2).strip()
        level = min(4, base + max(0, depth - 2) + 1)
        if text:
            headings.append((level, text))
    return headings


def _collect_entries(plan: list[dict]) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    for row in plan:
        section = row["section"]
        heading = str(section.heading or "").strip()
        if heading:
            entries.append((int(row["level"]), f"{row['number']} {heading}".strip()))
        if row["container"]:
            continue
        for level, text in _body_headings(row):
            if level <= 3:
                entries.append((level, text))
    return entries


def _add_cover(doc: Any, tender: Any) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    for _ in range(2):
        doc.add_paragraph()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(str(tender.title or "投标文件"))
    run.bold = True
    run.font.size = Pt(24)

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub_run = subtitle.add_run("投标文件")
    sub_run.bold = True
    sub_run.font.size = Pt(36)

    doc.add_paragraph()
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run(f"项目编号：{tender.code or ''}").font.size = Pt(12)
    if tender.buyer:
        buyer = doc.add_paragraph()
        buyer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        buyer.add_run(f"采购人：{tender.buyer}").font.size = Pt(12)

    doc.add_paragraph()
    bidder = doc.add_paragraph()
    bidder.alignment = WD_ALIGN_PARAGRAPH.CENTER
    bidder.add_run("投标人：____________________").font.size = Pt(14)

    date = doc.add_paragraph()
    date.alignment = WD_ALIGN_PARAGRAPH.CENTER
    from datetime import datetime

    date.add_run(datetime.now().strftime("日期：%Y 年 %m 月")).font.size = Pt(14)


def _add_toc(doc: Any, entries: list[tuple[int, str]], pages: dict[str, int]) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
    from docx.shared import Cm, Pt

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title.add_run("目录")
    title_run.bold = True
    title_run.font.size = Pt(18)

    for level, text in entries:
        paragraph = doc.add_paragraph()
        paragraph.paragraph_format.left_indent = Cm(0.72 * max(0, level - 1))
        paragraph.paragraph_format.line_spacing = 1.3
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            Cm(15.6), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
        )
        paragraph.add_run(text)
        page = pages.get(text)
        paragraph.add_run("\t" + (str(page) if page else ""))


def _add_table(doc: Any, rows: list[list[str]]) -> None:
    from docx.shared import Pt

    if not rows:
        return
    columns = max(len(row) for row in rows)
    table = doc.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    table.autofit = True
    for row_index, row in enumerate(rows):
        for column_index in range(columns):
            cell = table.cell(row_index, column_index)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            text = row[column_index] if column_index < len(row) else ""
            run = paragraph.add_run(text)
            run.font.size = Pt(10.5)
            if row_index == 0:
                run.bold = True
    doc.add_paragraph()


def _render_body(doc: Any, plan: list[dict], images: dict[str, bytes] | None = None) -> None:
    import io

    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Emu, Pt

    first = True
    for row in plan:
        section = row["section"]
        level = int(row["level"])
        heading = str(section.heading or "").strip()
        if level == 1 and not first:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        first = False
        if heading:
            doc.add_heading(f"{row['number']} {heading}".strip(), level=level)

        lines = str(row["body"] or "").splitlines()
        index = 0
        while index < len(lines):
            line = lines[index].rstrip()
            stripped = line.strip()
            if not stripped:
                index += 1
                continue
            if _MD_HR.match(stripped):
                index += 1
                continue
            matched_img = _MD_IMG.match(stripped)
            if matched_img:
                image_id = (matched_img.group(2) or "").rstrip("/").split("/")[-1]
                raw = (images or {}).get(image_id)
                paragraph = doc.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if raw:
                    try:
                        sec = doc.sections[-1]
                        width = Emu(
                            max(
                                int(sec.page_width) - int(sec.left_margin) - int(sec.right_margin),
                                1,
                            )
                        )
                        paragraph.add_run().add_picture(io.BytesIO(raw), width=width)
                    except Exception:  # noqa: BLE001 - 图片坏了不能拖垮整篇导出
                        paragraph.add_run("［图片］" + (matched_img.group(1) or ""))
                else:
                    paragraph.add_run(
                        "［图片］" + (matched_img.group(1) or "") + "（图片已不在库中）"
                    )
                index += 1
                continue
            if _MD_TABLE.match(stripped):
                block: list[list[str]] = []
                while index < len(lines) and _MD_TABLE.match(lines[index].strip()):
                    raw = lines[index].strip()
                    if not _MD_SEPARATOR.match(raw):
                        cells = [cell.strip() for cell in raw.strip("|").split("|")]
                        block.append(cells)
                    index += 1
                _add_table(doc, block)
                continue
            matched = _MD_HEAD.match(stripped)
            if matched:
                depth = len(matched.group(1))
                text = matched.group(2).strip()
                heading_level = min(4, level + max(0, depth - 2) + 1)
                if row["container"] and heading_level > level:
                    paragraph = doc.add_paragraph()
                    paragraph.add_run(text).bold = True
                else:
                    doc.add_heading(text, level=heading_level)
                index += 1
                continue
            matched = _MD_UL.match(line)
            if matched:
                paragraph = doc.add_paragraph(style="List Bullet")
                _add_runs(paragraph, matched.group(1).strip())
                index += 1
                continue
            matched = _MD_OL.match(line)
            if matched:
                paragraph = doc.add_paragraph()
                paragraph.paragraph_format.left_indent = Pt(21)
                paragraph.paragraph_format.first_line_indent = Pt(-21)
                _add_runs(paragraph, f"{matched.group(1)} {matched.group(2).strip()}")
                index += 1
                continue
            paragraph = doc.add_paragraph()
            _add_runs(paragraph, stripped)
            index += 1


def _build_package(
    tender: Any,
    plan: list[dict],
    entries: list[tuple[int, str]] | None = None,
    pages: dict[str, int] | None = None,
    total_pages: int | None = None,
) -> bytes:
    import io

    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()
    _setup_styles(doc)
    cover_section = doc.sections[0]
    _setup_page(cover_section, cover=True)

    _add_cover(doc, tender)

    if entries is not None:
        toc_section = doc.add_section(WD_SECTION.NEW_PAGE)
        _setup_page(toc_section)
        toc_section.header.is_linked_to_previous = False
        toc_section.footer.is_linked_to_previous = False
        _add_toc(doc, entries, pages or {})

    body_section = doc.add_section(WD_SECTION.NEW_PAGE)
    _setup_page(body_section)
    _set_page_number_start(body_section, 1)
    body_section.header.is_linked_to_previous = False
    body_section.footer.is_linked_to_previous = False

    header = body_section.header.paragraphs[0] if body_section.header.paragraphs else body_section.header.add_paragraph()
    header.text = f"{tender.title} 投标文件（{tender.code}）"
    header.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in header.runs:
        run.font.size = 9

    footer = body_section.footer.paragraphs[0] if body_section.footer.paragraphs else body_section.footer.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run("第 ")
    _add_field(footer, "PAGE", "1")
    footer.add_run(" 页  共 ")
    if total_pages:
        footer.add_run(str(total_pages))
    else:
        footer.add_run("  ")
    footer.add_run(" 页")
    for run in footer.runs:
        run.font.size = 9

    from zhibiao.images import collect_images

    _render_body(
        doc, plan, collect_images(str(row.get("body") or "") for row in plan)
    )

    buffer = io.BytesIO()
    try:
        from zhibiao.export_docx import normalize_theme_fonts

        normalize_theme_fonts(doc)
    except Exception:  # noqa: BLE001 - 字体归一失败不阻塞出件
        pass
    doc.save(buffer)
    return buffer.getvalue()


def _convert_pdf(docx_bytes: bytes) -> bytes | None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    with tempfile.TemporaryDirectory(prefix="zhibiao-pdf-") as tmp:
        root = Path(tmp)
        source = root / "source.docx"
        outdir = root / "out"
        outdir.mkdir()
        source.write_bytes(docx_bytes)
        env = {"HOME": str(root), "PATH": "/usr/local/bin:/usr/bin:/bin"}
        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation=file://{root / 'profile'}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(outdir),
                    str(source),
                ],
                check=True,
                timeout=240,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except Exception:
            return None
        target = outdir / "source.pdf"
        return target.read_bytes() if target.is_file() else None


def _pdf_info(pdf_bytes: bytes, entries: list[tuple[int, str]]) -> tuple[dict[str, int], int]:
    try:
        import pdfplumber
    except ImportError:
        return {}, 0
    pages: dict[str, int] = {}
    try:
        with pdfplumber.open(__import__("io").BytesIO(pdf_bytes)) as pdf:
            page_texts = [re.sub(r"\s+", "", page.extract_text() or "") for page in pdf.pages]
            total = len(pdf.pages)
    except Exception:
        return {}, 0
    for _, text in entries:
        needle = re.sub(r"\s+", "", text)
        if not needle:
            continue
        for index, page_text in enumerate(page_texts):
            if needle in page_text:
                pages[text] = max(1, index)
                break
    return pages, total


def export_document(path: Path, fmt: str, tender: Any, sections: list[Any]) -> None:
    plan = _section_numbering(sections)
    entries = _collect_entries(plan)
    probe = _build_package(tender, plan, entries=None)
    probe_pdf = _convert_pdf(probe)
    pages: dict[str, int] = {}
    total_pages = 0
    if probe_pdf:
        pages, probe_total = _pdf_info(probe_pdf, entries)
        # 探针文档只有封面 + 正文，正文页码从 1 开始；封面按 1 页计。
        total_pages = max(1, probe_total - 1)
    final = _build_package(tender, plan, entries=entries, pages=pages, total_pages=total_pages)
    if fmt == "docx":
        path.write_bytes(final)
        return
    pdf = _convert_pdf(final)
    if not pdf:
        raise RuntimeError("服务器缺少 LibreOffice，无法生成 PDF；请改用 DOCX 交付")
    path.write_bytes(pdf)
