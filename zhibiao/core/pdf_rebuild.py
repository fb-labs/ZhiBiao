"""把 PDF 原页「重建」成真 Word 内容（可填写页用）。

为什么要有这条路径：整页位图视觉最像原件，但客户**填不了字**——下划线是画在图片
里的，长度固定，字多了要么溢出要么压字。所以有填写位的页改成真段落：

  * 下划线换成**带下划线的红色全角空格**，客户一打字就继承「红色 + 下划线」，
    打多长线就多长，超出版心自动折行；
  * 行位置用「左缩进 = 原横坐标、段前距 = 原纵坐标、行距 = 原行距」做绝对定位，
    所以重建页和原件逐行对齐（实测 Δtop / Δx0 = 0.0pt）；
  * 仍然放在**页边距为 0** 的节里，和整页位图页共用同一套节的规则
    （页眉页脚不叠加、分页不重复）。

纯函数（`analyze_page` / `write_page`）不依赖 PDF 库以外的东西，便于单测。
"""
from __future__ import annotations

from typing import Any

from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor

EMU_PER_PT = 12700
FONT_MAP = {
    "fangsong": "仿宋",
    "simsun": "宋体",
    "songti": "宋体",
    "simhei": "黑体",
    "heiti": "黑体",
    "kaiti": "楷体",
    "microsoftyahei": "微软雅黑",
    "yahei": "微软雅黑",
    "timesnewroman": "Times New Roman",
    "arial": "Arial",
}
# 正文首行与页顶的距离修正：Word 把行盒顶部算在段前距之后，而 PDF 给的是字形顶部。
# 实测 8.5pt 时逐行 Δtop = 0.0，写死为常量，换纸张再校一次即可。
TOP_FUDGE_PT = 8.5


def font_of(name: str) -> str:
    """PDF 字体名 → 中文字体名（常带 `ABSEKN+` 这种子集前缀）。"""
    key = (name or "").split("+")[-1].replace(" ", "").lower()
    for token, cn in FONT_MAP.items():
        if token in key:
            return cn
    return "仿宋"


def _line_groups(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把词按纵坐标归成视觉行。"""
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        for line in lines:
            if abs(line["top"] - word["top"]) <= 4:
                line["words"].append(word)
                break
        else:
            lines.append({"top": word["top"], "words": [word]})
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["top"] = min(w["top"] for w in line["words"])
        line["bottom"] = max(w["bottom"] for w in line["words"])
    return sorted(lines, key=lambda ln: ln["top"])


def _table_boxes(page: Any) -> list[tuple[float, float, float, float]]:
    try:
        return [tuple(float(v) for v in table.bbox) for table in page.find_tables()]
    except Exception:  # noqa: BLE001
        return []


def _is_blank_line(line: tuple[float, float, float],
                   boxes: list[tuple[float, float, float, float]],
                   verticals: list[tuple[float, float, float]]) -> bool:
    """这条横线是不是「填写空位」的下划线。

    **表格边框也是横线**。不排除掉的话，表格页会被当成"有填写位"而重建成无框
    文字，报价表、资格审查表会当场散架。两条判据：落在表格 bbox 内的一律不算，
    两端有竖线穿过的一律不算（那是格线，不是下划线）。
    """
    x0, top, x1 = line
    if x1 - x0 < 20:
        return False
    for bx0, btop, bx1, bbottom in boxes:
        if bx0 - 2 <= x0 and x1 <= bx1 + 2 and btop - 2 <= top <= bbottom + 2:
            return False
    for vx, vtop, vbottom in verticals:
        if x0 - 2 <= vx <= x1 + 2 and vtop - 2 <= top <= vbottom + 2:
            return False
    return True


def analyze_page(page: Any) -> dict[str, Any]:
    """把一页拆成「行 + 下划线空位 + 行距 + 版心」，供 write_page 使用。"""
    words = page.extract_words(extra_attrs=["size", "fontname"])
    boxes = _table_boxes(page)
    table_blocks = _table_blocks(page, boxes)
    # 表格里的字不能再当正文行写一遍，否则内容重复一遍
    words = [w for w in words
             if not any(b[0] - 1 <= (w["x0"] + w["x1"]) / 2 <= b[2] + 1
                        and b[1] - 1 <= (w["top"] + w["bottom"]) / 2 <= b[3] + 1
                        for b in boxes)]
    lines = _line_groups(words)
    verticals = [(float(ln["x0"]), float(ln["top"]), float(ln["bottom"]))
                 for ln in page.lines if abs(ln["x0"] - ln["x1"]) < 1.5
                 and ln["bottom"] - ln["top"] > 5]
    underlines = []
    for ln in page.lines:
        if abs(ln["top"] - ln["bottom"]) >= 1.5:
            continue
        candidate = (float(ln["x0"]), float(ln["top"]), float(ln["x1"]))
        if _is_blank_line(candidate, boxes, verticals):
            underlines.append(candidate)
    tops = sorted(ln["top"] for ln in lines)
    pitches = [b - a for a, b in zip(tops, tops[1:]) if 10 < b - a < 40]
    pitch = sorted(pitches)[len(pitches) // 2] if pitches else 23.4

    counts: dict[tuple[str, float], int] = {}
    for line in lines:
        for word in line["words"]:
            key = (font_of(word["fontname"]), round(float(word["size"] or 12.0), 1))
            counts[key] = counts.get(key, 0) + len(word["text"])
    body_font, body_size = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ("仿宋", 12.0)

    slots = 0
    for line in lines:
        line_slots = [u for u in underlines
                      if line["top"] - 2 <= u[1] <= line["bottom"] + 14]
        line["slots"] = sorted(line_slots, key=lambda s: s[0])
        slots += len(line_slots)

    return {
        "width": float(page.width),
        "height": float(page.height),
        "lines": lines,
        "pitch": round(pitch, 1),
        "font": body_font,
        "size": body_size,
        "slots": slots,
        "tables": len(boxes),
        "table_blocks": table_blocks,
        "text_left": min((w["x0"] for ln in lines for w in ln["words"]), default=0.0),
        "text_right": max((w["x1"] for ln in lines for w in ln["words"]), default=float(page.width)),
    }


def _table_blocks(page: Any, boxes: list[tuple[float, float, float, float]]) -> list[dict[str, Any]]:
    """把每张表解析成「列宽 + 行高 + 合并关系 + 每格文字」，供 write_page 生成真 w:tbl。"""
    blocks: list[dict[str, Any]] = []
    try:
        tables = page.find_tables()
    except Exception:  # noqa: BLE001
        return blocks
    for table in tables:
        try:
            blocks.append(_one_table(page, table))
        except Exception:  # noqa: BLE001 —— 单张表解析失败就让整页回落位图
            return []
    return blocks


def _one_table(page: Any, table: Any) -> dict[str, Any]:
    rows = list(table.rows)
    n_rows = len(rows)
    if n_rows == 0:
        raise ValueError("空表")

    # 去重取「唯一的单元格 bbox」。合并格在 pdfplumber 里只给一个 bbox，被它盖住的
    # 格子是 None，所以行列范围不能靠 (行,列) 去重推，只能由 bbox 反推。
    boxes: dict[tuple[float, ...], None] = {}
    for row in rows:
        for cell in row.cells:
            if cell is None:
                continue
            boxes.setdefault(tuple(round(float(v), 1) for v in cell), None)

    # 列边界：所有单元格左右边的去重集合。不能用"每列 min~max"：合并格 bbox 跨列，
    # 会把列宽算大（实测 78pt 的列算成 141.7pt），累计漂移 58pt。
    raw_edges = sorted({v for box in boxes for v in (box[0], box[2])})
    edges: list[float] = []
    for value in raw_edges:
        if not edges or value - edges[-1] > 1.0:
            edges.append(value)
    widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    if len(widths) < 2:
        raise ValueError("列数异常")

    row_tops: list[float] = []
    heights: list[float] = []
    for row in rows:
        bbox = getattr(row, "bbox", None)
        if bbox:
            row_tops.append(float(bbox[1]))
            heights.append(float(bbox[3]) - float(bbox[1]))
        else:
            row_tops.append((row_tops[-1] + 24.0) if row_tops else 0.0)
            heights.append(24.0)

    def span_of(values: list[float], start: float, end: float) -> tuple[int, int]:
        first = 0
        for index, value in enumerate(values):
            if value <= start + 0.6:
                first = index
            else:
                break
        last = first
        for index, value in enumerate(values):
            if value < end - 0.6:
                last = index
        return first, last

    cells = []
    for box in boxes:
        c0, c1 = span_of(edges, box[0], box[2])
        r0, r1 = span_of(row_tops, box[1], box[3])
        c1 = min(c1, len(widths) - 1)
        r1 = min(r1, n_rows - 1)
        if c1 < c0 or r1 < r0:
            continue
        text, size = _cell_text(page, box)
        cells.append({"r0": r0, "c0": c0, "r1": r1, "c1": c1, "text": text, "size": size})
    return {"bbox": [float(v) for v in table.bbox], "rows": n_rows,
            "widths": widths, "heights": heights, "cells": cells}


def _fit_size(text: str, width_pt: float, size: float) -> float:
    """格子里的字太长就缩字号，别让它折行把行撑高。

    折行会让行高从 21pt 涨到 30pt 以上，一张 15 行的表能长出 33pt（实测）。
    中文按 1 字宽 = 字号，西文按 0.55 倍估。
    """
    if not text or width_pt <= 4:
        return size
    units = 0.0
    for ch in text:
        units += 1.0 if ord(ch) > 0x2E7F else 0.55
    needed = units * size
    if needed <= width_pt - 2:
        return size
    return max(6.0, round(size * (width_pt - 2) / needed, 1))

def _cell_text(page: Any, bbox: tuple[float, ...]) -> tuple[str, float]:
    x0, top, x1, bottom = bbox
    picked = [w for w in page.extract_words(extra_attrs=["size"])
              if w["x0"] >= x0 - 1 and w["x1"] <= x1 + 1
              and w["top"] >= top - 2 and w["bottom"] <= bottom + 2]
    picked.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
    text = "".join(w["text"] for w in picked)
    size = max((float(w["size"] or 10.0) for w in picked), default=10.0)
    return text, round(size, 1)


# 一页上超过这么多空位，多半是把格线/装饰线误判成了空位：宁可保位图，不乱重建。
MAX_SLOTS_PER_PAGE = 40


def is_fillable(analysis: dict[str, Any]) -> bool:
    """这页该不该重建成真 Word 内容。

    两种情况重建：
      * **有表格**且表格全都解析成功（报价表、资格审查表要能填数字）；
      * **有下划线空位**且没有表格（投标函、授权书这类纯文字表单）。

    表格解析失败时整页回落位图 —— 宁可填不了，也不能把表格拆散。
    """
    tables = int(analysis.get("tables") or 0)
    blocks = analysis.get("table_blocks") or []
    if tables:
        return len(blocks) == tables
    slots = int(analysis.get("slots") or 0)
    return 0 < slots <= MAX_SLOTS_PER_PAGE


def _is_footer_line(line: dict[str, Any], lines: list[dict[str, Any]]) -> bool:
    """页脚印的页码（单独一个数字、且在页面最后一行）：丢掉，页码由平台统一出。"""
    if len(lines) < 3 or line is not lines[-1]:
        return False
    texts = [w["text"].strip() for w in line["words"] if w["text"].strip()]
    return len(texts) == 1 and texts[0].isdigit()


def line_offset(size: float, body_size: float) -> float:
    """固定行距下行盒把字垂直居中：字号越大，字顶离行盒顶越近。

    12pt 实测 8.5pt，15.9pt 标题实测 5.4pt，按 0.79pt/pt 线性拟合。
    """
    return max(0.0, TOP_FUDGE_PT - (float(size) - float(body_size)) * 0.79)


def write_page(doc: Any, analysis: dict[str, Any], *, break_before: bool = False,
               trim_top_pt: float = 0.0) -> list[Any]:
    """把 analyze_page 的结果写成段落 + 真表格，返回写进去的顶层块（段落/表格）。

    `break_before` 为真时首个块「段前分页」——和整页位图页的做法一致，
    不用空段落分页，章与章之间不会多出白纸。

    `trim_top_pt` 只在线上成稿里扣：Word 在「段前分页 / 分节后的第一段」上方会**多留
    一整行**（实测 20.3pt，紧跟分节符的首页是 18.6pt）。单页校准脚本里没有这一行，所以
    不扣——校准仍按原件绝对定位比对。
    """
    lines = list(analysis.get("lines") or [])
    tables = list(analysis.get("table_blocks") or [])
    pitch = float(analysis.get("pitch") or 23.4)
    body_font = str(analysis.get("font") or "仿宋")
    body_size = float(analysis.get("size") or 12.0)
    text_right = float(analysis.get("text_right") or float(analysis.get("width") or 595.3))
    page_w = float(analysis.get("width") or 595.3)

    blocks: list[tuple[str, dict[str, Any]]] = [("line", ln) for ln in lines]
    blocks += [("table", tb) for tb in tables]
    blocks.sort(key=lambda item: (item[1]["top"] if item[0] == "line" else item[1]["bbox"][1]))

    written: list[Any] = []
    prev_top: float | None = None
    prev_offset: float | None = None
    prev_para: Any = None          # 上一个段落：表格要靠它的段后距定位
    prev_bottom: float | None = None
    first = True
    for kind, block in blocks:
        if kind == "table":
            para = _write_table(doc, block, body_font, body_size,
                                top_gap_from=prev_bottom, prev_top=prev_top, prev_offset=prev_offset, pitch=pitch,
                                first=first, break_before=break_before,
                                trim_top_pt=trim_top_pt)
            written.append(para)
            prev_top = float(block["bbox"][3])
            prev_offset = None
            prev_bottom = float(block["bbox"][3])
            prev_para = None
            first = False
            continue
        line = block
        if _is_footer_line(line, lines):
            continue
        line_size = max((float(w["size"] or body_size) for w in line["words"]),
                        default=body_size)
        offset = line_offset(line_size, body_size)
        para = doc.add_paragraph()
        fmt = para.paragraph_format
        fmt.line_spacing = Pt(pitch)
        fmt.space_after = Pt(0)
        fmt.space_before = Pt(0)
        if first:
            fmt.space_before = Pt(max(0.0, round(
                float(line["top"]) - offset - float(trim_top_pt or 0.0), 1)))
            fmt.page_break_before = bool(break_before)
        elif prev_offset is None and prev_bottom is not None:
            # 上一块是表格：间距从表格下边界算起（实测表格会比设定高度长出 3.5pt）
            fmt.space_before = Pt(max(0.0, round(
                float(line["top"]) - prev_bottom - offset - TABLE_BOTTOM_GROWTH_PT, 1)))
        else:
            base = float(line["top"]) - float(prev_top or 0.0) - pitch
            correction = offset - float(prev_offset if prev_offset is not None else offset)
            fmt.space_before = Pt(max(0.0, round(base - correction, 1)))
        prev_top = float(line["top"])
        prev_offset = offset
        line_left = float(line["words"][0]["x0"])
        for slot in line.get("slots") or []:
            line_left = min(line_left, float(slot[0]))
        fmt.left_indent = Emu(int(max(0.0, line_left) * EMU_PER_PT))
        fmt.right_indent = Emu(int(max(0.0, page_w - text_right) * EMU_PER_PT))
        _emit_line(para, line, body_font, body_size)
        written.append(para)
        prev_para = para
        prev_bottom = float(line["bottom"])
        first = False
    return written


# 表格比设定高度略高（单元格上下内边距），实测 3.5pt；表格上方还要留 7pt
TABLE_BOTTOM_GROWTH_PT = 3.5
TABLE_TOP_FUDGE_PT = 0.0


def _write_table(doc: Any, block: dict[str, Any], body_font: str, body_size: float, *,
                 top_gap_from: float | None, prev_top: float | None, prev_offset: float | None, pitch: float,
                 first: bool, break_before: bool, trim_top_pt: float = 0.0) -> Any:
    """按原坐标生成真 `w:tbl`：列宽、行高、合并、左边距全都照原件。"""
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_ROW_HEIGHT_RULE
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement

    bbox = [float(v) for v in block["bbox"]]
    widths = [float(v) for v in block["widths"]]
    heights = [float(v) for v in block["heights"]]
    cells = list(block["cells"])
    n_rows = int(block["rows"])
    n_cols = len(widths)

    # 表格上方必须有段落承载"段前距"：Word 表格自己不能上间距。
    if first:
        spacer = doc.add_paragraph()
        sfmt = spacer.paragraph_format
        sfmt.line_spacing = Pt(1)
        sfmt.space_before = Pt(max(0.0, round(
            bbox[1] - 1.0 - float(trim_top_pt or 0.0), 1)))
        sfmt.space_after = Pt(0)
        sfmt.page_break_before = bool(break_before)
        spacer.add_run("")
        prev = spacer
    else:
        prev = doc.paragraphs[-1] if doc.paragraphs else None
        if prev is not None and prev_top is not None:
            # 上一段真正的下边界 = 「行盒顶端 + 行距」，而行盒顶端 = 文字 top − 偏移修正。
            # 用文字底边算会把间距多算十几到二十几 pt（实测表格整体偏低 13~24pt）。
            line_box_top = float(prev_top) - float(prev_offset or 0.0)
            gap = bbox[1] - (line_box_top + float(pitch)) + TABLE_TOP_FUDGE_PT
            prev.paragraph_format.space_after = Pt(max(0.0, round(gap, 1)))

    table = doc.add_table(rows=n_rows, cols=n_cols)
    table.style = "Table Grid"
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    # 表格默认贴左边距（本节是 0），不给 tblInd 就整张表左移 89.9pt
    indent = OxmlElement("w:tblInd")
    indent.set(qn("w:w"), str(int(bbox[0] * 20)))
    indent.set(qn("w:type"), "dxa")
    tbl_pr.append(indent)
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tbl_pr.append(layout)
    # 单元格左右内边距清零：默认 0.19cm 会把居中文字整体推偏 5.4pt
    margins = OxmlElement("w:tblCellMar")
    for side in ("left", "right"):
        edge = OxmlElement("w:%s" % side)
        edge.set(qn("w:w"), "0")
        edge.set(qn("w:type"), "dxa")
        margins.append(edge)
    tbl_pr.append(margins)

    for c, width in enumerate(widths):
        for r in range(n_rows):
            table.cell(r, c).width = Emu(int(width * EMU_PER_PT))
    for r in range(n_rows):
        height = heights[r] if r < len(heights) else 24.0
        if height <= 1:
            continue
        # 只用 API 设一次行高：之前同时写 row.height 和手工追加 trHeight，
        # 会在 trPr 里留下两个 trHeight，Word 按"至少"叠加，整表长出 46~51pt。
        row = table.rows[r]
        row.height = Emu(int(height * EMU_PER_PT))
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST

    for cell in cells:                      # 合并要先做，再写字
        if cell["r1"] > cell["r0"] or cell["c1"] > cell["c0"]:
            table.cell(cell["r0"], cell["c0"]).merge(table.cell(cell["r1"], cell["c1"]))

    for cell in cells:
        target = table.cell(cell["r0"], cell["c0"])
        target.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        # 合并会把两个格子各自的空段落都留下，多出来的空段落直接顶出一行的高度
        # （实测 p92 的「联系方式」跨两行，行高从 50.6 涨到 76.9）。
        tc = target._tc
        for extra in tc.findall(qn("w:p"))[1:]:
            tc.remove(extra)
        para = target.paragraphs[0]
        # 表头居中，数据格左对齐（和原件一致）；空格的格保持空，客户直接往里填
        para.alignment = (WD_ALIGN_PARAGRAPH.CENTER if cell["r0"] == 0
                          else WD_ALIGN_PARAGRAPH.LEFT)
        para.paragraph_format.space_before = Pt(0)
        para.paragraph_format.space_after = Pt(0)
        text = str(cell.get("text") or "")
        span_pt = sum(widths[c] for c in range(cell["c0"], cell["c1"] + 1))
        cell_size = _fit_size(text, span_pt, float(cell.get("size") or body_size))
        # 单元格内行距必须定死：默认 1.08 倍会把行撑高，8 行表格累计长出 50pt
        para.paragraph_format.line_spacing = Pt(round(cell_size * 1.35, 1))
        run = para.add_run(text)
        size = float(cell.get("size") or body_size)
        run.font.size = Pt(round(min(max(cell_size, 6.0), 20.0), 1))
        run.font.name = body_font
        run._element.rPr.rFonts.set(qn("w:eastAsia"), body_font)
    return table


def _emit_line(para: Any, line: dict[str, Any], body_font: str, body_size: float) -> None:
    """一行 → 「文字 run / 红色下划线空位 run」，按横坐标顺序写入。

    PDF 里下划线空位那块**没有文字**（只有一条线），所以空位不能靠遍历词发现，
    只能按坐标插到「它左边最后一个词」之后。
    """
    words = line["words"]
    ordered = sorted(line.get("slots") or [], key=lambda s: s[0])
    cursor = [0]

    def flush_before(x_limit: float) -> None:
        while cursor[0] < len(ordered) and ordered[cursor[0]][0] < x_limit - 1:
            x0, _top, x1 = ordered[cursor[0]]
            count = max(2, int(round((x1 - x0) / body_size)))
            run = para.add_run("\u3000" * count)
            run.font.size = Pt(body_size)
            run.font.name = body_font
            run._element.rPr.rFonts.set(qn("w:eastAsia"), body_font)
            run.font.underline = True
            run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
            cursor[0] += 1

    index = 0
    while index < len(words):
        word = words[index]
        flush_before(word["x0"])
        group = [word]
        index += 1
        while (index < len(words)
               and words[index]["x0"] - group[-1]["x1"] < 4
               and not any(abs(s[0] - words[index]["x0"]) <= 2 for s in ordered)):
            group.append(words[index])
            index += 1
        run = para.add_run("".join(w["text"] for w in group))
        size = float(group[0]["size"] or body_size)
        font = font_of(group[0]["fontname"])
        run.font.size = Pt(round(size, 1))
        run.font.name = font
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font)
    flush_before(10 ** 6)