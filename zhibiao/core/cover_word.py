"""Conservative, XML-preserving Word template-cover extraction.

No generated cover and no document-global state live here. ``geometry`` uses the
python-docx section attribute names and EMU lengths; ``section_xml`` additionally
preserves docGrid, orientation, vertical alignment and other section settings.
The caller owns section/page boundaries. Unknown placeholders remain untouched.
"""
from __future__ import annotations

import copy
import io
import re
from collections import Counter
from typing import Any

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from . import format_template as ft

_TITLES = {"投标文件", "投标书", "响应文件"}
_FORMAT = re.compile(r"^(?:第[一二三四五六七八九十百零〇\d]+[章节部分][:：、]?)?(?:投标文件格式|投标书格式|响应文件格式|投标文件组成)$")
_CHAPTER = re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章节部分]")
_NUMBERED = re.compile(r"^(?:[一二三四五六七八九十百]+[、.．]|[（(][一二三四五六七八九十百\d]+[）)]|\d+[、.．])")
_BODY_TITLE = re.compile(r"^(?:投标函(?:及.*)?|响应函|法定代表人身份证明|身份证明|授权委托书|资格审查资料|商务.*偏差表|技术.*偏差表|报价表|投标报价.*|开标一览表|投标人须知|评标办法|合同条款)$")
_BODY_PROSE = re.compile(r"我方|我单位|我公司|郑重承诺|特此承诺|遵守|应当|必须|不得|致[:：]|尊敬的|兹授权")
_FIELD = re.compile(r"项目名称|工程名称|采购项目|项目编号|招标编号|采购编号|投标人|供应商|响应人|日期|年.*月.*日|盖章|签章")
_PLACEHOLDER = re.compile(r"[（(【](项目名称|工程名称|采购项目名称|招标项目名称|项目编号|招标编号|采购编号|投标人名称|供应商名称|响应人名称)[）)】]")
_PAREN = re.compile(r"[（(【]([^（）()【】\n]{1,32})[）)】]")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_GEOMETRY = ("page_width", "page_height", "top_margin", "bottom_margin", "left_margin", "right_margin", "header_distance", "footer_distance", "gutter")


def _plain(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text or "")


def _text(element: Any) -> str:
    return ft.element_text(element)


def _open(raw: bytes) -> Any:
    document = docx.Document(io.BytesIO(ft._as_docx(bytes(raw))))
    ft.normalize_symbols(document.element.body)
    return document


def _on(node: Any) -> bool:
    return node is not None and node.get(qn("w:val"), "true") not in {"0", "false", "off"}


def _page_before(element: Any, document: Any) -> bool:
    if element.tag != qn("w:p"):
        return False
    paragraph = Paragraph(element, document._body)
    if paragraph.paragraph_format.page_break_before is not None:
        return bool(paragraph.paragraph_format.page_break_before)
    for style in _chain(paragraph.style):
        value = style.paragraph_format.page_break_before
        if value is not None:
            return bool(value)
    return False


def _split_breaks(element: Any) -> list[Any | None]:
    """Split a paragraph even if a page break shares a run with visible text.

    None represents a hard page boundary. All other run XML (drawings, notes,
    tabs, mixed sizes, etc.) stays in its original order and formatting.
    """
    def marker(node: Any) -> bool:
        return (node.tag == qn("w:br") and node.get(qn("w:type")) == "page") or node.tag == qn("w:lastRenderedPageBreak")

    def split(node: Any) -> list[Any | None]:
        if marker(node):
            return [None]
        if not any(marker(desc) for desc in node.iter()):
            return [copy.deepcopy(node)]
        parts: list[Any | None] = []
        shell = copy.deepcopy(node)
        for child in list(shell):
            shell.remove(child)
        prop_tag = qn("w:pPr") if node.tag == qn("w:p") else qn("w:rPr") if node.tag == qn("w:r") else None
        props = [child for child in node if child.tag == prop_tag]
        current = copy.deepcopy(shell)
        for prop in props:
            current.append(copy.deepcopy(prop))
        for child in node:
            if child.tag == prop_tag:
                continue
            for piece in split(child):
                if piece is None:
                    parts.extend((current, None))
                    current = copy.deepcopy(shell)
                    for prop in props:
                        current.append(copy.deepcopy(prop))
                else:
                    current.append(piece)
        parts.append(current)
        return parts

    if element.tag != qn("w:p"):
        return [copy.deepcopy(element)]
    if not any(marker(node) for node in element.iter()):
        return [copy.deepcopy(element)]
    result = split(element)
    # Empty fragments created solely by the delimiter are not original blanks.
    return [piece for piece in result if piece is None or _text(piece) or any(piece.iter(qn("w:drawing"))) or any(piece.iter(qn("w:pict")))]


def _units(document: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page = 0
    for index, element in enumerate(document.element.body):
        if element.tag == qn("w:sectPr"):
            continue
        if _page_before(element, document):
            page += 1
        for piece in _split_breaks(element):
            if piece is None:
                page += 1
                continue
            # Original sectPr is recorded separately, not left inside a slice.
            for section in list(piece.iter(qn("w:sectPr"))):
                section.getparent().remove(section)
            result.append({"element": piece, "index": index, "page": page})
        if element.find("./" + qn("w:pPr") + "/" + qn("w:sectPr")) is not None:
            page += 1
    return result


def _toc(element: Any) -> bool:
    if element.tag != qn("w:p"):
        return False
    styles = element.findall("./" + qn("w:pPr") + "/" + qn("w:pStyle"))
    if any(re.search(r"toc|目录", node.get(qn("w:val"), ""), re.I) for node in styles):
        return True
    return any("TOC" in (node.text or "").upper() for node in element.iter(qn("w:instrText")))


def _stop(text: str) -> bool:
    return text in {"目录", "目次"} or bool(_CHAPTER.match(text) or _NUMBERED.match(text) or _BODY_TITLE.fullmatch(text))


def _find_cover(units: list[dict[str, Any]]) -> tuple[int, int] | None:
    scope = False
    toc_page: int | None = None
    best: tuple[int, int, int] | None = None
    for pos, unit in enumerate(units):
        element = unit["element"]
        text = _plain(_text(element))
        if text in {"目录", "目次"}:
            toc_page = unit["page"]
            scope = False
            continue
        if toc_page is not None and unit["page"] != toc_page:
            toc_page = None
        if _FORMAT.fullmatch(text) and not _toc(element) and toc_page is None:
            # A real chapter following a table of contents can start without an
            # explicit page break; short exact headings do not contain leaders.
            scope = True
            toc_page = None
            continue
        if _CHAPTER.match(text):
            scope = False
        if element.tag != qn("w:p") or text not in _TITLES or _toc(element) or toc_page is not None:
            continue
        start = pos
        # Include pre-title project lines and every original blank, but never a
        # chapter heading, previous body, or a different physical page.
        while start > 0 and units[start - 1]["page"] == unit["page"]:
            previous = units[start - 1]["element"]
            prior = _plain(_text(previous))
            if _stop(prior) or _toc(previous) or prior in _TITLES or _BODY_PROSE.search(prior) or len(prior) > 100:
                break
            start -= 1
        end = pos + 1
        while end < len(units) and units[end]["page"] == unit["page"]:
            next_text = _plain(_text(units[end]["element"]))
            if _stop(next_text) or next_text in _TITLES or _toc(units[end]["element"]):
                break
            end += 1
        rows = [_plain(_text(item["element"])) for item in units[start:end]]
        nonempty = [row for row in rows if row]
        joined = "\n".join(nonempty)
        fields = sum(bool(_FIELD.search(row)) for row in nonempty)
        placeholder = bool(_PLACEHOLDER.search(joined))
        if (len(nonempty) < 3 or len(nonempty) > 16 or len(joined) > 450 or
                any(len(row) > 110 for row in nonempty) or _BODY_PROSE.search(joined) or
                fields < 2 or not (scope or placeholder)):
            continue
        score = int(scope) * 4 + int(placeholder) * 3 + min(fields, 4)
        if best is None or score > best[0]:
            best = (score, start, end)
    return (best[1], best[2]) if best else None


def _section(document: Any, index: int) -> Any:
    """A section is described by the *following* sectPr, not the preceding one."""
    chosen = None
    inherited: dict[str, Any] = {}
    for pos, child in enumerate(document.element.body):
        section = child if child.tag == qn("w:sectPr") else child.find("./" + qn("w:pPr") + "/" + qn("w:sectPr"))
        if section is None:
            continue
        for prop in section:
            inherited[prop.tag] = copy.deepcopy(prop)
        if pos >= index:
            chosen = copy.deepcopy(section)
            for tag, prop in inherited.items():
                if chosen.find(tag) is None:
                    chosen.append(copy.deepcopy(prop))
            break
    if chosen is None:
        chosen = copy.deepcopy(document.sections[-1]._sectPr)
    for tag in ("w:headerReference", "w:footerReference"):
        for reference in chosen.findall(qn(tag)):
            chosen.remove(reference)
    return chosen


def _geometry(document: Any, section: Any) -> dict[str, Any]:
    from docx.section import Section
    wrapped = Section(section, document.part)
    geometry: dict[str, Any] = {name: int(value) for name in _GEOMETRY if (value := getattr(wrapped, name, None)) is not None}
    geometry["orientation"] = int(wrapped.orientation)
    return geometry


def _chain(style: Any) -> list[Any]:
    result = []
    seen = set()
    while style is not None and style.style_id not in seen:
        seen.add(style.style_id)
        result.append(style)
        style = style.base_style
    return list(reversed(result))


def _merge(base: Any, overlay: Any) -> None:
    if overlay is None:
        return
    for key, value in overlay.attrib.items():
        base.set(key, value)
    for child in overlay:
        existing = base.find(child.tag)
        if existing is None:
            base.append(copy.deepcopy(child))
        else:
            _merge(existing, child)


def _materialize(document: Any) -> None:
    """Reuse the engine, then complete its paragraph/character inheritance.

    Compute effective XML *before* calling the shared helper: default character
    styles must not overwrite a paragraph style's font, nor explicit mixed runs.
    Attribute-wise merging preserves e.g. inherited East Asian fonts alongside
    direct Latin fonts. All non-font paragraph properties (grid, tabs, spacing,
    borders, indents, underline, etc.) are materialized as well.
    """
    defaults = document.styles.element.find(qn("w:docDefaults"))
    default_p = defaults.find("./" + qn("w:pPrDefault") + "/" + qn("w:pPr")) if defaults is not None else None
    default_r = defaults.find("./" + qn("w:rPrDefault") + "/" + qn("w:rPr")) if defaults is not None else None
    saved = []
    for element in document.element.body.iter(qn("w:p")):
        paragraph = Paragraph(element, document._body)
        ppr, rbase = OxmlElement("w:pPr"), OxmlElement("w:rPr")
        _merge(ppr, default_p)
        _merge(rbase, default_r)
        for style in _chain(paragraph.style):
            _merge(ppr, style.element.find(qn("w:pPr")))
            _merge(rbase, style.element.find(qn("w:rPr")))
        _merge(ppr, element.find(qn("w:pPr")))
        runs = []
        for run in element.iter(qn("w:r")):
            effective = copy.deepcopy(rbase)
            direct = run.find(qn("w:rPr"))
            style_ref = direct.find(qn("w:rStyle")) if direct is not None else None
            if style_ref is not None:
                try:
                    for style in _chain(document.styles[style_ref.get(qn("w:val"))]):
                        _merge(effective, style.element.find(qn("w:rPr")))
                except KeyError:
                    pass
            _merge(effective, direct)
            runs.append((run, effective))
        saved.append((element, ppr, runs))
    ft.materialize_styles(document)
    for element, ppr, runs in saved:
        existing = element.find(qn("w:pPr"))
        if existing is not None:
            element.remove(existing)
        element.insert(0, ppr)
        for run, effective in runs:
            existing = run.find(qn("w:rPr"))
            if existing is not None:
                run.remove(existing)
            run.insert(0, effective)


def _strip_boundaries(element: Any) -> None:
    for node in list(element.iter()):
        if node.tag in {qn("w:sectPr"), qn("w:pageBreakBefore"), qn("w:lastRenderedPageBreak")} or (node.tag == qn("w:br") and node.get(qn("w:type")) == "page"):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)


def _keep_heading_space(body: Any, units: list[dict[str, Any]], start: int,
                        document: Any = None) -> None:
    """Keep the skipped format-chapter heading's height as an empty spacer.

    The heading owns the top of the cover page. Its text must not be printed
    again (our own chapter follows), but dropping the paragraph lifts every
    cover row by one heading line (31pt on the case 4 tender). Keeping the
    paragraph with its own properties and no runs preserves the tender's own
    y for every row, including the space its style contributes.
    """
    if start <= 0:
        return
    heading = units[start - 1]
    text = _plain(_text(heading["element"]))
    if heading["page"] != units[start]["page"]:
        return
    if not (_CHAPTER.match(text) or _FORMAT.fullmatch(text)):
        return
    spacer = copy.deepcopy(heading["element"])
    for child in list(spacer):
        if child.tag in {qn("w:r"), qn("w:hyperlink"), qn("w:bookmarkStart"),
                         qn("w:bookmarkEnd")}:
            spacer.remove(child)
    _strip_boundaries(spacer)
    body.insert(0, spacer)


def word_cover_plan(raw: bytes, filename: str = "") -> dict[str, Any] | None:
    """Return only an actual sparse bid-template cover, never a body fallback.

    The standalone DOCX retains its original package (including image and note
    relationships); only body XML is selected. ``filename`` is informational;
    .doc conversion is delegated to format_template._as_docx.
    """
    if not raw:
        return None
    try:
        document = _open(raw)
    except Exception:
        return None
    units = _units(document)
    selected = _find_cover(units)
    if selected is None:
        return None
    start, end = selected
    section = _section(document, units[start]["index"])
    geometry = _geometry(document, section)
    body = document.element.body
    for child in list(body):
        body.remove(child)
    for unit in units[start:end]:
        element = copy.deepcopy(unit["element"])
        _strip_boundaries(element)
        body.append(element)
    _keep_heading_space(body, units, start, document)
    body.append(copy.deepcopy(section))
    _materialize(document)
    # Do not retain chapter/background images that were outside the cover XML.
    ft._prune_parts(document)
    output = io.BytesIO()
    document.save(output)
    return {"kind": "word", "docx": output.getvalue(), "geometry": geometry,
            "section_xml": section.xml, "source": "word-template"}


# Only complete bracketed field labels are replaceable. Facts may use either
# these Chinese keys or the documented application-style English aliases.
_FACT_KEYS = {
    "项目名称": ("项目名称", "project_name"),
    "工程名称": ("工程名称", "project_name"),
    "采购项目名称": ("采购项目名称", "project_name"),
    "招标项目名称": ("招标项目名称", "project_name"),
    "项目编号": ("项目编号", "project_code", "project_no"),
    "招标编号": ("招标编号", "tender_code", "tender_no"),
    "采购编号": ("采购编号", "procurement_no"),
    "投标人名称": ("投标人名称", "bidder_name", "company_name"),
    "供应商名称": ("供应商名称", "supplier_name", "bidder_name", "company_name"),
    "响应人名称": ("响应人名称", "bidder_name", "company_name"),
    "招标人名称": ("招标人名称", "tenderer_name", "buyer_name"),
    "采购人名称": ("采购人名称", "buyer_name", "tenderer_name"),
}


def _replace_placeholders(element: Any, facts: dict) -> None:
    for paragraph in element.iter(qn("w:p")):
        nodes = list(paragraph.iter(qn("w:t")))
        text = "".join(node.text or "" for node in nodes)
        offsets = []
        cursor = 0
        for node in nodes:
            length = len(node.text or "")
            offsets.append((cursor, cursor + length, node))
            cursor += length
        for match in reversed(list(_PAREN.finditer(text))):
            label = _plain(match.group(1))
            aliases = _FACT_KEYS.get(label, ())
            value = next((str(facts[key]) for key in aliases if facts.get(key) is not None and str(facts[key]).strip()), None)
            if value is None:
                continue
            affected = [(start, stop, node) for start, stop, node in offsets if stop > match.start() and start < match.end()]
            # Put replacement in the first actual label run, not a standalone
            # opening-parenthesis run; keep the surrounding mixed runs intact.
            anchor = next((node for start, stop, node in affected if stop > match.start() + 1), affected[0][2])
            for start, stop, node in affected:
                old = node.text or ""
                left = max(0, match.start() - start)
                right = min(stop - start, match.end() - start)
                node.text = old[:left] + (value if node is anchor else "") + old[right:]
                node.set(_XML_SPACE, "preserve")


def _copy_part(source: Any, package: Any, memo: dict) -> Any:
    """Copy a note's related part graph, including nested picture relations."""
    from docx.opc.part import Part
    if source in memo:
        return memo[source]
    target = Part(ft._unique_partname(package, source.partname), source.content_type, source.blob, package)
    memo[source] = target
    for rel in source.rels.values():
        if rel.is_external:
            target.rels.add_relationship(rel.reltype, rel.target_ref, rel.rId, True)
        else:
            target.rels.add_relationship(rel.reltype, _copy_part(rel.target_part, package, memo), rel.rId)
    return target


def _carry_notes(source: Any, document: Any, elements: list[Any]) -> None:
    """append_to_document handles r:id; foot/endnotes use w:id instead."""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.part import Part
    from lxml import etree
    for name, reltype in (("footnote", RT.FOOTNOTES), ("endnote", RT.ENDNOTES)):
        refs = [node for element in elements for node in element.iter(qn("w:" + name + "Reference"))]
        if not refs:
            continue
        source_rel = next((rel for rel in source.part.rels.values() if rel.reltype == reltype), None)
        if source_rel is None:
            continue
        source_root = etree.fromstring(source_rel.target_part.blob)
        target_rel = next((rel for rel in document.part.rels.values() if rel.reltype == reltype), None)
        if target_rel is None:
            root = copy.deepcopy(source_root)
            for child in list(root):
                root.remove(child)
            part = Part(ft._unique_partname(document.part.package, source_rel.target_part.partname), source_rel.target_part.content_type, b"", document.part.package)
            document.part.relate_to(part, reltype)
        else:
            part = target_rel.target_part
            root = etree.fromstring(part.blob)
        used = {int(node.get(qn("w:id"))) for node in root if node.get(qn("w:id")) is not None}
        wanted = {node.get(qn("w:id")) for node in refs}
        mapping = {}
        memo: dict[Any, Any] = {}
        for note in source_root:
            old = note.get(qn("w:id"))
            if old not in wanted and int(old or 0) > 0:
                continue
            if int(old or 0) <= 0 and int(old or 0) in used:
                continue
            new_id = int(old or 0)
            if new_id in used:
                new_id = max(used | {0}) + 1
            used.add(new_id)
            clone = copy.deepcopy(note)
            clone.set(qn("w:id"), str(new_id))
            mapping[old] = str(new_id)
            for node in clone.iter():
                for attr in ("r:id", "r:embed", "r:link"):
                    rid = node.get(qn(attr))
                    rel = source_rel.target_part.rels.get(rid) if rid else None
                    if rel is not None:
                        new_rid = part.relate_to(rel.target_ref, rel.reltype, True) if rel.is_external else part.relate_to(_copy_part(rel.target_part, document.part.package, memo), rel.reltype)
                        node.set(qn(attr), new_rid)
            root.append(clone)
        part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        for node in refs:
            old = node.get(qn("w:id"))
            if old in mapping:
                node.set(qn("w:id"), mapping[old])


def append_word_cover(doc: Any, plan: dict, facts: dict) -> list[Any]:
    """Append preserved cover XML, without page breaks or section properties.

    Bare labels, underscores, project names and signing instructions are never
    replaced. Missing facts leave their explicit bracketed placeholders intact.
    """
    if not plan or plan.get("kind") != "word" or not plan.get("docx"):
        return []
    source = docx.Document(io.BytesIO(plan["docx"]))
    elements = ft.append_to_document(doc, plan["docx"], keep_sections=False)
    for element in elements:
        _strip_boundaries(element)
        _replace_placeholders(element, facts or {})
    _carry_notes(source, doc, elements)
    return elements


def word_style_plan(raw: bytes, filename: str = "") -> dict[str, Any] | None:
    """Return only first-page typography/geometry, never source cover wording."""
    if not raw:
        return None
    try:
        document = _open(raw)
    except Exception:
        return None
    units = _units(document)
    if not units:
        return None
    first_page = units[0]["page"]
    first = []
    for unit in units:
        if unit["page"] != first_page or len(first) >= 30:
            break
        first.append(unit)
    section = _section(document, first[0]["index"])
    body = document.element.body
    for child in list(body):
        body.remove(child)
    for unit in first:
        body.append(copy.deepcopy(unit["element"]))
    body.append(copy.deepcopy(section))
    _materialize(document)
    fonts: Counter = Counter()
    sizes: Counter = Counter()
    title = None
    alignments: Counter = Counter()
    for paragraph in document.paragraphs:
        if not _plain(paragraph.text):
            continue
        alignments[int(paragraph.alignment) if paragraph.alignment is not None else 0] += 1
        for run in paragraph.runs:
            if not _plain(run.text):
                continue
            rpr = run._r.find(qn("w:rPr"))
            rfonts = rpr.find(qn("w:rFonts")) if rpr is not None else None
            name = (rfonts.get(qn("w:eastAsia")) or rfonts.get(qn("w:ascii"))) if rfonts is not None else run.font.name
            size = run.font.size.pt if run.font.size is not None else None
            weight = len(_plain(run.text))
            if name:
                fonts[name] += weight
            if size:
                sizes[float(size)] += weight
                if title is None or size > title[0]:
                    title = (float(size), name)
    if not sizes and not fonts:
        return None
    style = {"font_name": fonts.most_common(1)[0][0] if fonts else None,
             "font_size_pt": sizes.most_common(1)[0][0] if sizes else None,
             "title_font_name": title[1] if title else None,
             "title_font_size_pt": title[0] if title else None,
             "alignment": alignments.most_common(1)[0][0] if alignments else 0}
    return {"kind": "word-style", "source": "word-style", "style": style,
            "geometry": _geometry(document, section), "section_xml": section.xml}
