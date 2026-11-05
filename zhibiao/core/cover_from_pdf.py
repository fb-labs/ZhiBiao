"""Request-local cover plans. PDF geometry is independent of body layout.

Only a sparse page with a standalone bid-document title is a cover template.
A section page/TOC reference is never used as evidence of a cover. No document,
paragraph or project data is retained at module scope.
"""
from __future__ import annotations

import copy
import io
import math
import re
from typing import Any
from zhibiao.core.cover_budget import bounded_cover_work

# A tender template's own fill-in slot that nobody can fill from the data we
# have. Printed instructions like （盖章）/（签章） are deliberately absent: they
# are part of the form, not something to complete.
_UNFILLED = re.compile(
    r"[（(]\s*(?:项目名称|招标编号|项目编号|投标人名称|供应商名称|单位名称|投标单位)"
    r"\s*[）)]")


def _plain(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _rows(page: Any) -> list[dict[str, Any]]:
    from zhibiao.core.pdf_rebuild import font_of

    words = sorted(page.extract_words(extra_attrs=["size", "fontname"]) or [],
                   key=lambda w: (float(w["top"]), float(w["x0"])))
    rows: list[dict[str, Any]] = []
    for word in words:
        if not rows or abs(float(word["top"]) - rows[-1]["top"]) > 5:
            rows.append({"top": float(word["top"]), "words": []})
        rows[-1]["words"].append(dict(word))
    for row in rows:
        row["words"].sort(key=lambda w: float(w["x0"]))
        row["bottom"] = max(float(w["bottom"]) for w in row["words"])
        row["text"] = "".join(w["text"] for w in row["words"])
        row["size"] = max(float(w["size"]) for w in row["words"])
        row["font"] = font_of(row["words"][0]["fontname"])
        row["x0"] = min(float(w["x0"]) for w in row["words"])
        row["x1"] = max(float(w["x1"]) for w in row["words"])
        row["centered"] = abs((row["x0"] + row["x1"]) / 2 - float(page.width) / 2) < 15
        row["rules"] = [(float(ln["x0"]), float(ln["x1"]), float(ln["top"]))
                        for ln in page.lines
                        if abs(float(ln["top"]) - float(ln["bottom"])) < 1
                        and row["top"] <= float(ln["top"]) <= row["bottom"] + 5
                        and float(ln["x1"]) - float(ln["x0"]) > 15]
    return rows


def _is_template(rows: list[dict[str, Any]]) -> bool:
    texts = [_plain(row["text"]) for row in rows]
    return (len(rows) <= 16 and len("".join(texts)) <= 400
            and any(t in ("投标文件", "投标书", "响应文件") for t in texts)
            and any(re.search(r"投标人|供应商|法定代表人|年月日|盖.*章", t) for t in texts)
            and not any(re.match(r"^(?:\d+[.、]|[一二三四五六七八九十]+、).*投标函", t)
                        for t in texts))


def _cover_rows(page: Any) -> list[dict[str, Any]]:
    rows = []
    for row in _rows(page):
        text = _plain(row["text"])
        if not text or re.fullmatch(r"\d{1,4}", text):
            continue
        if re.fullmatch(r"第[一二三四五六七八九十百\d]+章.*(?:投标|响应)(?:文件|书)格式", text):
            continue
        if text in ("目录", "目次"):
            break
        rows.append(row)
    return rows


def _style_fallback(page: Any, *, title: str, names: list[str]) -> list[dict[str, Any]]:
    """Reuse this tender's visual hierarchy, not its purchaser's identity/signature."""
    rows = _cover_rows(page)
    banners = [r for r in rows if _plain(r["text"]) in ("招标文件", "招标书", "采购文件")]
    if not banners:
        return []
    banner = banners[0]
    before = [r for r in rows if r["top"] < banner["top"]
              and not re.search(r"编号|日期|[:：]", r["text"])]
    project_rows = []
    # Some tender covers put the project-owning unit above a larger project
    # name. Preserve both font hierarchies ONLY when both are part of this
    # project's current official name, never by copying an unrelated party.
    prefix = _plain(before[0]["text"]) if len(before) > 1 else ""
    if prefix and _plain(title).startswith(prefix):
        project_rows.append(dict(before[0], text=prefix, replace_text=prefix, centered=True))
        remainder = _plain(title)[len(prefix):]
        if remainder:
            basis = max(before[1:], key=lambda r: r["size"])
            project_rows.append(dict(basis, text=remainder, replace_text=remainder, centered=True))
    else:
        basis = max(before, key=lambda r: r["size"]) if before else banner
        project_rows.append(dict(basis, text=title, replace_text=title, centered=True))
    for project in project_rows:
        # Keep the tender's own alignment: the tendering unit above a centered
        # project name is usually flush left, and centering it moves the line
        # ~65pt sideways against the original cover.
        center = (float(project["x0"]) + float(project["x1"])) / 2
        if abs(center - float(page.width) / 2) <= 25:
            project["x0"], project["x1"] = 55.0, float(page.width) - 55.0
        else:
            project["centered"] = False
    result = project_rows + [dict(banner, text="投标文件", replace_text="投标文件", centered=True)]
    agency_block = False
    for row in rows:
        text = _plain(row["text"])
        if text.startswith("招标代理"):
            agency_block = True
            continue
        if re.match(r"^(?:法定代表人|负责人|委托代理人)", text):
            if not agency_block:
                # Keep the tender's own label and its （签章）; the blank left for
                # the signature becomes an underline, never an invented name.
                result.append(dict(copy.deepcopy(row), fill_gap=True))
            continue
        if re.search(r"(?:项目|招标)编号", text):
            result.append(copy.deepcopy(row))
        elif re.match(r"招标(?:单位|人)[:：]", text):
            # Only the role word changes（招标单位→投标人）. The value area stays
            # empty and underlined so it is visibly a fill-in line: putting the
            # platform's project label there produced sentences the tender never
            # contained, and an internal 「{{待填:…}}」 token leaked into the file.
            # The label often arrives one glyph per word (letterspaced cover), so
            # it is rebuilt as a single run instead of editing words[0].
            words = row["words"]
            suffix = [dict(w) for w in words
                      if re.search(r"[（(][^）)]*(?:章|签字)[^）)]*[）)]", str(w["text"]))]
            label = dict(words[0])
            label["text"] = "投标人："
            label["x1"] = float(label["x0"]) + _text_width(label["text"], float(row["size"]))
            new_row = dict(row, words=[label] + suffix,
                           text="".join(str(w["text"]) for w in [label] + suffix),
                           fill_gap=True)
            new_row.pop("replace_text", None)
            result.append(new_row)
        elif re.match(r"(?:日期[:：]?)?\d{4}年\d{1,2}月", text) or text.startswith("日期"):
            result.append(dict(row, text="年    月    日", replace_text="年    月    日"))
    return sorted(result, key=lambda r: r["top"])


@bounded_cover_work
def select_pdf_cover(source: bytes, *, title: str, names: list[str] | None = None,
                     max_pages: int = 400) -> dict[str, Any] | None:
    """Scan once, while the PDF is open; return pure data, no open-page references."""
    import pdfplumber

    if not source or not bytes(source).lstrip().startswith(b"%PDF-"):
        return None
    with pdfplumber.open(io.BytesIO(source)) as pdf:
        for index, page in enumerate(pdf.pages[:max_pages]):
            # Cheap text check avoids table analysis/word grouping on ordinary pages.
            text = _plain(page.extract_text() or "")
            if len(text) > 450 or not any(t in text for t in ("投标文件", "投标书", "响应文件")):
                continue
            rows = _cover_rows(page)
            if not _is_template(rows):
                continue
            skip = {index}
            for following in range(index + 1, min(index + 4, len(pdf.pages))):
                head = _plain(pdf.pages[following].extract_text() or "")
                if not head.startswith(("目录", "目次")):
                    break
                skip.add(following)
            return {"kind": "pdf", "source": "pdf-template", "rows": rows,
                    "width": float(page.width), "height": float(page.height),
                    "skip_pages": skip, "page": index + 1}
        first = pdf.pages[0]
        rows = _style_fallback(first, title=title, names=names or [])
        if not rows:
            return None
        return {"kind": "pdf", "source": "tender-style", "rows": rows,
                "width": float(first.width), "height": float(first.height),
                "skip_pages": set()}


def substitute(text: str, facts: dict[str, str]) -> str:
    """Replace explicit slots only, never a label or another project's literals."""
    result = str(text or "")
    for key, value in sorted(facts.items(), key=lambda p: len(p[0]), reverse=True):
        if value and (key.startswith(("（", "(", "{{"))):
            result = result.replace(key, str(value))
    return result


def _text_width(text: str, size: float) -> float:
    return sum(size * (0.5 if ch.isascii() else 1.0) for ch in text)


def append_pdf_cover(doc: Any, plan: dict[str, Any], facts: dict[str, str]) -> list[Any]:
    """Source coordinates, explicit line heights, tab-positioned words and rules.

    A dedicated full-page cover section is applied by the caller. Top gaps are
    measured from the previous line box, NOT by adding PDF absolute coordinates.
    Long filled titles wrap inside their reserved region without moving later rows.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
    from docx.shared import Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    width, height = float(plan["width"]), float(plan["height"])
    rows = copy.deepcopy(plan["rows"])
    cursor = 0.0
    elements = []
    for i, row in enumerate(rows):
        size = float(row["size"])
        pitch = size * 1.25
        top = float(row["top"])
        text = str(row.get("replace_text", row["text"]))
        filled = substitute(text, facts)
        replaced = filled != row["text"] or "replace_text" in row
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.left_indent = Pt(0)
        pf.right_indent = Pt(0)
        pf.first_line_indent = Pt(0)
        pf.space_after = Pt(0)
        pf.keep_with_next = False
        pf.keep_together = True
        pf.widow_control = False
        pf.page_break_before = False
        pf.line_spacing = Pt(pitch)
        # Glyph top offset for exact line boxes, calibrated by actual Word rendering.
        offset = (pitch - size) / 2 + size * 0.12
        # Word adds its first line-box origin on a zero-margin first page.
        # Remove that origin once (not once per row); verified against native
        # Word PDF glyph coordinates, independent of subsequent body spacing.
        pf.space_before = Pt(max(0.0, top - offset - cursor - (17.0 if i == 0 else 0.0)))
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        ppr = p._p.get_or_add_pPr()
        snap = OxmlElement("w:snapToGrid")
        snap.set(qn("w:val"), "0")
        ppr.append(snap)

        def run(value: str, sz: float = size, font: str = row["font"], underline: bool = False):
            r = p.add_run(value)
            r.font.name = font
            r.font.size = Pt(round(sz * 2) / 2)
            r.font.bold = False
            r.font.color.rgb = RGBColor(0, 0, 0)
            r.font.underline = underline
            r._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), font)
            if "待填" in value or _UNFILLED.search(value):
                r.font.color.rgb = RGBColor(0xC0, 0, 0)
            elif replaced and filled != text:
                r.font.color.rgb = RGBColor(0x8B, 0, 0)
            return r

        if replaced:
            left = max(35.0, float(row["x0"]))
            right = min(width - 35.0, float(row["x1"]))
            # Filled project names can be longer than the original slot. Use its
            # centered column, not a compressed font or a second cover title.
            if row.get("centered"):
                left, right = 55.0, width - 55.0
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pf.left_indent, pf.right_indent = Pt(left), Pt(width - right)
            run(filled)
            count = max(1, math.ceil(_text_width(filled, size) / max(1.0, right - left)))
        else:
            # Keep word x positions as real tabs; spaces alone cannot preserve a
            # long signature gap or the spaced document title.
            words = row["words"]
            x0 = float(words[0]["x0"])
            pf.left_indent = Pt(x0)
            previous_x1 = x0
            for wi, word in enumerate(words):
                x = float(word["x0"])
                if wi and x - previous_x1 > 1.5:
                    underline = bool(row.get("fill_gap")) or any(
                        a <= previous_x1 + 3 and b >= x - 3
                        for a, b, _y in row.get("rules", []))
                    # Word tab positions are measured from the section's left
                    # margin, NOT from this paragraph's left indent.
                    pf.tab_stops.add_tab_stop(Pt(x), WD_TAB_ALIGNMENT.LEFT,
                                              WD_TAB_LEADER.LINES if underline else WD_TAB_LEADER.SPACES)
                    run("\t")
                from zhibiao.core.pdf_rebuild import font_of
                emitted = run(word["text"], float(word["size"]), font_of(word["fontname"]))
                value = word["text"]
                if len(value) > 1 and not any(ch.isascii() for ch in value):
                    nominal = round(float(word["size"]) * 2) / 2
                    delta = float(word["x1"]) - x - nominal * len(value)
                    if abs(delta) > 0.2:
                        # Embedded PDF fonts may use tighter CJK tracking than
                        # Word's installed font. Preserve the source word width.
                        spacing = OxmlElement("w:spacing")
                        spacing.set(qn("w:val"), str(round(delta * 20 / (len(value) - 1))))
                        emitted._element.get_or_add_rPr().append(spacing)
                previous_x1 = float(word["x1"])
            count = 1
        cursor = max(cursor, top - offset) + pitch * count
        if cursor > height - 12:
            raise ValueError("封面内容超过一页，请检查源模板封面边界")
        elements.append(p._p)
    return elements


def default_cover_plan(title: str, facts: dict[str, str], style: dict[str, Any] | None = None
                       ) -> dict[str, Any]:
    """Only for documents without an available source layout; no example data."""
    style = style or {}
    font = str(style.get("font") or style.get("font_name") or "宋体")
    rows = []
    data = [(title, 170.0, float(style.get("font_size_pt") or 14.0)),
            ("投标文件", 280.0, float(style.get("title_size") or style.get("title_font_size_pt") or 22))]
    for label in ("招标编号", "项目编号", "招标人"):
        if facts.get(label):
            data.append((label + "：" + facts[label], 380.0 + 32 * (len(data) - 2), 14.0))
    data += [("投标人：{{待填:投标人全称}}", 620.0, 14.0),
             ("{{待填:投标日期（年月日）}}", 710.0, 14.0)]
    for text, top, size in data:
        rows.append({"text": text, "replace_text": text, "top": top, "size": size,
                     "font": font, "x0": 55, "x1": 540, "centered": True})
    return {"kind": "pdf", "source": "default-style", "rows": rows,
            "width": 595.276, "height": 841.89, "skip_pages": set()}


def finish_cover_section(doc: Any, plan: dict[str, Any]) -> Any:
    """One next-page section boundary; no extra empty page-break paragraph."""
    from docx.enum.section import WD_SECTION_START
    from docx.shared import Pt

    body_geometry = {name: getattr(doc.sections[0], name) for name in
                     ("page_width", "page_height", "top_margin", "bottom_margin", "left_margin",
                      "right_margin", "header_distance", "footer_distance", "orientation", "gutter")}
    doc.add_section(WD_SECTION_START.NEW_PAGE)
    cover, body = doc.sections[0], doc.sections[1]
    if plan.get("section_xml"):
        from lxml import etree
        from docx.oxml.ns import qn
        source_section = etree.fromstring(plan["section_xml"].encode("utf-8"))
        for child in list(cover._sectPr):
            cover._sectPr.remove(child)
        for child in source_section:
            if child.tag not in (qn("w:headerReference"), qn("w:footerReference")):
                cover._sectPr.append(copy.deepcopy(child))
    geometry = plan.get("geometry") or {
        "page_width": Pt(plan.get("width", 595.276)), "page_height": Pt(plan.get("height", 841.89)),
        "top_margin": 0, "bottom_margin": 0, "left_margin": 0, "right_margin": 0,
        "header_distance": 0, "footer_distance": 0}
    for name, value in geometry.items():
        if name in body_geometry:
            setattr(cover, name, value)
    for name, value in body_geometry.items():
        setattr(body, name, value)
    # Stop the section-break paragraph itself consuming a normal 26pt body line.
    marker = doc.paragraphs[-1]
    marker.paragraph_format.space_before = Pt(0)
    marker.paragraph_format.space_after = Pt(0)
    marker.paragraph_format.line_spacing = Pt(1)
    marker.add_run().font.size = Pt(1)
    return cover._sectPr


def clear_cover_furniture(doc: Any) -> None:
    """Detach body furniture first so clearing cover never removes body headers."""
    if len(doc.sections) < 2:
        return
    for attr in ("header", "footer", "first_page_header", "first_page_footer",
                 "even_page_header", "even_page_footer"):
        getattr(doc.sections[1], attr).is_linked_to_previous = False
        part = getattr(doc.sections[0], attr)
        part.is_linked_to_previous = False
        for p in part.paragraphs:
            p.clear()


def locate_cover_page(source: bytes, **_kwargs: Any) -> int:
    plan = select_pdf_cover(source, title="")
    return int((plan or {}).get("page") or 0)


def cover_lines_from_pdf(source: bytes, page_no: int) -> list[dict[str, Any]]:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(source)) as pdf:
        return _cover_rows(pdf.pages[page_no - 1]) if 1 <= page_no <= len(pdf.pages) else []


def build_cover_from_pdf(doc: Any, lines: list[dict[str, Any]], facts: dict[str, str]) -> list[Any]:
    return append_pdf_cover(doc, {"rows": lines, "width": doc.sections[0].page_width.pt,
                                  "height": doc.sections[0].page_height.pt}, facts)


def _neutralize_host_spacing(doc: Any, elements: list[Any]) -> None:
    """Do not let this document's own default paragraph spacing stretch the cover.

    A generated document starts from the python-docx default template, whose
    ``docDefaults`` carry ``after=200 twips`` and ``line=276``. Those defaults
    are invisible to the template's own package and inflate every blank template
    line by ~10pt, which alone pushes a one-page cover onto a second page.
    Paragraphs that really declare spacing (directly or via their style) keep it.
    """
    from docx.shared import Pt
    from docx.text.paragraph import Paragraph

    for element in elements:
        if getattr(element, "tag", None) is None:
            continue
        try:
            paragraph = Paragraph(element, doc._body)
        except Exception:  # noqa: BLE001 - tables/images are not paragraphs
            continue
        pf = paragraph.paragraph_format
        if pf.line_spacing is None:
            pf.line_spacing = 1.0  # w:line=240 lineRule=auto → natural single line
        if pf.space_before is None:
            pf.space_before = Pt(0)
        if pf.space_after is None:
            pf.space_after = Pt(0)


def _word_cover_height(doc: Any, elements: list[Any]) -> tuple[float, list[tuple[Any, float]]]:
    """Approximate rendered height so a template cover can be kept on one page."""
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    cover = doc.sections[0]
    grid = cover._sectPr.find(qn("w:docGrid"))
    pitch = (int(grid.get(qn("w:linePitch"))) / 20.0
             if grid is not None and grid.get(qn("w:linePitch")) else None)
    total = 0.0
    parts: list[tuple[Any, float]] = []
    for element in elements:
        try:
            paragraph = Paragraph(element, doc._body)
        except Exception:  # noqa: BLE001
            continue
        pf = paragraph.paragraph_format
        size = None
        mark = element.find(qn("w:pPr"))
        mark = mark.find(qn("w:rPr")) if mark is not None else None
        if mark is not None:
            sz = mark.find(qn("w:sz"))
            size = int(sz.get(qn("w:val"))) / 2 if sz is not None else None
        if size is None:
            style = paragraph.style
            while style is not None and size is None:
                if style.font.size is not None:
                    size = style.font.size.pt
                style = style.base_style
        size = size or 10.5
        spacing = pf.line_spacing
        if spacing is None:
            height = size * 1.32
        elif isinstance(spacing, (int, float)):
            height = size * 1.32 * float(spacing)
        else:
            height = float(spacing.pt)
        if pitch:
            height = max(1, math.ceil(height / pitch - 1e-6)) * pitch
        height += ((pf.space_before.pt if pf.space_before is not None else 0.0)
                   + (pf.space_after.pt if pf.space_after is not None else 0.0))
        total += height
        parts.append((element, height))
    return total, parts


def fit_word_cover_one_page(doc: Any, elements: list[Any]) -> int:
    """Keep a template cover on its first page without touching any real text.

    1. Blank lines after the template's last real line only hold the gap before
       the template's own TOC, which this export replaces with its own TOC, so
       they are always dropped.
    2. If the cover still cannot fit (a filled official project name can wrap to
       two or three lines), blank lines are removed bottom-up. Text, fonts,
       sizes, order, rules and signature gaps are never modified.
    """
    from docx.oxml.ns import qn

    def has_content(element: Any) -> bool:
        if "".join(t.text or "" for t in element.iter(qn("w:t"))).strip():
            return True
        return any(next(element.iter(qn(tag)), None) is not None
                   for tag in ("w:drawing", "w:pict", "w:object"))

    def removable(element: Any) -> bool:
        if element.find('.//' + qn("w:sectPr")) is not None:
            return False
        if has_content(element):
            return False
        # An underline in a blank line draws a place to sign: keep it unless it
        # sits after the last real line, where it can only be a decorative gap.
        return not any(r.find(qn("w:rPr")) is not None
                       and r.find(qn("w:rPr")).find(qn("w:u")) is not None
                       for r in element.iter(qn("w:r")))

    def drop(element: Any) -> bool:
        parent = element.getparent()
        if parent is None or len(parent.findall(qn("w:p"))) <= 2:
            return False
        parent.remove(element)
        return True

    last = max((i for i, element in enumerate(elements) if has_content(element)), default=-1)
    removed = 0
    for element in elements[last + 1:]:
        if drop(element):
            removed += 1
    elements = [element for element in elements if element.getparent() is not None]

    cover = doc.sections[0]
    available = (cover.page_height.pt - cover.top_margin.pt - cover.bottom_margin.pt)
    total, parts = _word_cover_height(doc, elements)
    for element, height in reversed(parts):
        if total <= available / 1.04:
            break
        if not removable(element) or not drop(element):
            continue
        total -= height
        removed += 1
    return removed


def build_cover_plan(doc: Any, plan: dict[str, Any], facts: dict[str, str]) -> list[Any]:
    if plan.get("kind") == "word":
        from zhibiao.core.cover_word import append_word_cover
        elements = append_word_cover(doc, plan, facts)
        _neutralize_host_spacing(doc, elements)
        return elements
    return append_pdf_cover(doc, plan, facts)


# No reset/restore lists: callers keep the returned elements in their own request.
