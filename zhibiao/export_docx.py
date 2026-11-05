"""投标文件 DOCX 写入器（由 3001/zhinbiao2 导出逻辑复刻）。

包含：封面、目录与页码回填、页眉页脚、章节编号、Markdown 表格/图片、
招标原件 Word 切片、PDF 原页嵌入、页面几何切换。
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from typing import Any

import docx
from docx.oxml.ns import qn
from fastapi import HTTPException

from zhibiao.core.bid_integrity import (
    is_instruction_only,
    is_manual_content,
    repair_generated_text,
    requires_authored_content,
    source_backed_section,
)
from zhibiao.core.image_slots import (
    BOX_TITLE,
    IMAGE_PLACEHOLDER_RE,
    has_image_slot,
    image_slot_hint,
    is_image_slot_line,
    slot_lines,
)

logger = logging.getLogger(__name__)


DEFAULT_FORMAT_CFG: dict[str, Any] = {
    "toc": 1,
    "toc_level": 3,
    "body_font": "宋体",
    "body_size": 12.0,
    "line_spacing": 0,
    "margin_cm": 2.54,
    "header": 1,
    "cover": 1,
    "page_number": 1,
    "words_per_page": 800,
}


def _load_format_cfg() -> dict[str, Any]:
    """本项目采用固定排版基线（与 3001 默认值一致）。"""
    return dict(DEFAULT_FORMAT_CFG)


def _words_per_page() -> int:
    return 1200


# Word 里 w:asciiTheme / w:eastAsiaTheme 的优先级高于 w:ascii / w:eastAsia。
# 本模板 settings 的 w:themeFontLang 东亚语言是 ja-JP，主题字体解析到晚日的
# 「ＭＳ ゴシック」，西文落到 Calibri —— 标题中文全部是日文字形，与正文宋体
# 明显不一致（实测每一级标题都这样）。出件前统一改回显式字体。
_THEME_FONT_ATTRS = ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme")
_FORCE_FONT_STYLES = (
    "Normal",
    "Heading 1",
    "Heading 2",
    "Heading 3",
    "Heading 4",
    "TOC Heading",
    "TOC 1",
    "TOC 2",
    "TOC 3",
)


def normalize_theme_fonts(document: Any, font: str = "宋体") -> None:
    """去掉主题字体属性并给标题/正文样式补上显式宋体，避免 Word 换成日文字体。"""
    from docx.oxml import OxmlElement as _El

    roots = [document.styles.element]
    try:
        roots.append(document.element)
    except Exception:  # noqa: BLE001 - 取不到正文根节点时只处理样式
        pass
    for root in roots:
        for rfonts in root.iter(qn("w:rFonts")):
            for attr in _THEME_FONT_ATTRS:
                key = qn(attr)
                if key in rfonts.attrib:
                    del rfonts.attrib[key]

    for name in _FORCE_FONT_STYLES:
        try:
            style = document.styles[name]
        except Exception:  # noqa: BLE001 - 模板没有该样式就跳过
            continue
        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = _El("w:rFonts")
            rpr.append(rfonts)
        for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            rfonts.set(qn(attr), font)

    try:
        lang = document.settings.element.find(qn("w:themeFontLang"))
        if lang is not None:
            lang.set(qn("w:eastAsia"), "zh-CN")
    except Exception:  # noqa: BLE001 - 老版本 python-docx 没有 settings
        pass


def convert_bytes_soffice(data: bytes, src_ext: str, target: str) -> bytes:
    """把内存文档交给 LibreOffice 无头转换（docx → doc / pdf）。"""
    soffice = shutil.which("soffice")
    if not soffice:
        raise HTTPException(
            status_code=400,
            detail=f"服务器未安装 LibreOffice，暂不能导出 {target}，请先选择 docx 导出",
        )
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, f"src.{src_ext}")
        with open(src, "wb") as fh:
            fh.write(data)
        try:
            proc = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation=file://{os.path.join(td, 'profile')}",
                    "--convert-to",
                    target,
                    "--outdir",
                    td,
                    src,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "HOME": td},
            )
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=400, detail="LibreOffice 转换超时，请改用 docx 导出") from exc
        out = os.path.join(td, f"src.{target}")
        if not os.path.exists(out):
            msg = (proc.stderr or proc.stdout or "").strip()[:200]
            raise HTTPException(
                status_code=400, detail=f"LibreOffice 转换失败：{msg or '未知原因'}"
            )
        with open(out, "rb") as fh:
            return fh.read()


def _ocr_bounded(image: Any, lang: str, preprocess: str = "none") -> tuple[str, str]:
    """扫描页 OCR 兜底；环境没有 pytesseract 时返回空文本，由调用方降级为整页图片。"""
    try:
        import pytesseract  # type: ignore

        text = pytesseract.image_to_string(image, lang=lang)
        return str(text or "").strip(), "OCR"
    except Exception:  # noqa: BLE001 - 缺 OCR 组件时不让导出失败
        return "", "OCR 组件不可用"


# ============ Markdown → Word 渲染 ============
# 大模型生成的章节正文是 Markdown（标题 / 粗体 / 列表 / 表格）。
# 导出时若按行原样写入，Word 里会看到字面的 "##"、"**"、"|" 等标记，因此这里做真正的转换。
_MD_HEAD = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MD_UL = re.compile(r"^([ \t]{0,3})[-*+][ \t]+(.+)$")
_MD_OL = re.compile(r"^([ \t]{0,3})(\d{1,3})[.)][ \t]+(.+)$")
_MD_HR = re.compile(r"^[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
_MD_QUOTE = re.compile(r"^[ \t]{0,3}>[ \t]?(.*)$")
_MD_TBL = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_MD_TBL_SEP = re.compile(r"^[ \t]*\|?[ \t:|-]*-{3,}[ \t:|-]*\|?[ \t]*$")
# 行内：**粗体** / `代码` / [文字](链接) / ![图](链接)
_MD_INLINE = re.compile(r"(\*\*[^*\n]+\*\*|`[^`\n]+`|!?\[[^\]\n]*\]\([^)\n]+\))")
# 整行只是一张图（允许前后空白）：插入的图片独立成段
_MD_IMG = re.compile(r"^[ \t]*!\[([^\]\n]*)\]\(([^)\n]+)\)[ \t]*$")
# Only deliberate standalone image-needed lines become Word image slots; ordinary prose stays intact.
# 占位框语法（新写法 `{{图片占位:具体说明}}` 与两种旧写法）只从
# `app.core.image_slots` 取，导出侧不再自己维护一套正则（前端 imageSlots.ts 与它逐字对应）。
# 投标人待填内容：导出时整段标红，方便出件前逐个找出来填。
_FILL_TOKEN = re.compile(r"\{\{待填[:：][^}]*\}\}")
# re.split 必须带捕获组，否则待填 token 会被当成分隔符丢掉（实测正文里红色待填整段消失）
# 插图占位标记也一并在这里切出来：行内出现的标记不会画图框，但要标红并去掉花括号。
_FILL_TOKEN_SPLIT = re.compile(r"(\{\{待填[:：][^}]*\}\}|\{\{图片占位[:：][^}\r\n]*\}\})")
_INSERTED_IMAGE = re.compile(
    r"!\[[^\]\r\n]*\]\(/api/images/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\)"
)


def _chapter_image_status(content: str | None) -> str:
    """Pending slots take precedence over already-inserted Markdown images."""
    text = content or ""
    if has_image_slot(text):
        return "pending"
    return "complete" if _INSERTED_IMAGE.search(text) else "none"


def _md_img_width(doc) -> Any:
    """图片宽度 = 正文可用宽度（页宽 - 左右页边距），避免插进来的图撑出版心。"""
    from docx.shared import Emu
    try:
        # 按「正文所在的节」算：封面是第一节（页边距 0），正文在最后一节。
        # 用 sections[0] 会把图按整张纸宽插进去，右半边压到页边距外面。
        sec = doc.sections[-1]
        avail = int(sec.page_width) - int(sec.left_margin) - int(sec.right_margin)
        return Emu(max(int(avail), 1))
    except Exception:  # noqa: BLE001
        from docx.shared import Cm
        return Cm(15)


def _md_norm_title(s: str) -> str:
    """归一化标题用于重复判断：去掉「一、」「1.」「2.2.1」这类前置编号与空白。

    旧实现只剥单段编号，正文小标题被归一成「1.1.1 项目背景与建设目标」后就剥不掉了，
    Word 会在章节标题下多出一行重复标题（实测 20 章）。这里复用 chapter_rules 里
    与质量门禁同一套严格编号规则，保证「重复标题跳过」不再失效。
    """
    body = (s or "").strip()
    from zhibiao.core.chapter_rules import SUBHEADING_NUMBER_RE
    match = SUBHEADING_NUMBER_RE.match(body)
    return (body[match.end():] if match else body).strip()


def _md_add_runs(p, text: str) -> None:
    """把一行里的行内 Markdown 写成 Word run（粗体 / 等宽代码 / 链接文字）。

    `{{待填:…}}` 一律写成**红色**run：出件前需要人工补的内容必须一眼可见，
    旧实现把它当普通文字，混在正文里根本找不到（实测一份技术标 90+ 处）。
    行内出现的 `{{图片占位:…}}`（没独立成行，画不了图框）同样标红并去掉花括号，
    绝不让原始标记漏到 Word 里。
    """
    from docx.shared import RGBColor as _RGB
    for chunk in _FILL_TOKEN_SPLIT.split(text):
        if not chunk:
            continue
        if _FILL_TOKEN.fullmatch(chunk):
            run = p.add_run(chunk)
            try:
                run.font.color.rgb = _RGB(0xC0, 0x00, 0x00)
            except Exception:  # noqa: BLE001 - 取不到颜色属性也不能丢文字
                pass
            continue
        if IMAGE_PLACEHOLDER_RE.fullmatch(chunk):
            run = p.add_run("%s 此处插入：%s" % (BOX_TITLE, image_slot_hint(chunk)))
            try:
                run.font.color.rgb = _RGB(0xFF, 0x00, 0x00)
            except Exception:  # noqa: BLE001
                pass
            continue
        for tok in _MD_INLINE.split(chunk):
            if not tok:
                continue
            if tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
                p.add_run(tok[2:-2]).bold = True
            elif tok.startswith("`") and tok.endswith("`") and len(tok) > 2:
                p.add_run(tok[1:-1]).font.name = "Consolas"
            elif tok.startswith("[") and "](" in tok:
                p.add_run(tok[1:tok.index("]")])
            elif tok.startswith("![") and "](" in tok:
                continue  # 图片无法内嵌，直接跳过，避免留下 ![alt](url) 字样
            else:
                p.add_run(tok)


def _image_slot_borders(slot) -> None:
    """给占位框显式画红色单线边框（不依赖模板里有没有 Table Grid 样式）。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = slot._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement("w:" + edge)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "8")            # 1pt
        element.set(qn("w:space"), "0")
        element.set(qn("w:color"), "FF0000")     # 红框：出件前一眼看到「这里要放图」
        borders.append(element)
    properties.append(borders)


def _md_add_image_slot(doc, line: str) -> None:
    """待插入图片的占位框：1×1 带框表格 + 红字说明，用户直接把图片粘进去即可。

    需求原话：「待插入的图片弄一个 1X1 的表格，里面用来插图片，在文档里用来占位」。
    行高按版面给足，避免用户插图后整页排版被挤动。

    文案口径统一由 `app/core/image_slots.py` 提供（导出/前端/生成同一份）：
      [待插入图片] 此处插入：<从上下文推断出的具体要求>
      尺寸建议：宽 15cm 以内，需清晰可辨
    说明文字一律 RGB ff0000，边框也是红色：旧实现用灰色小字，混在正文里根本看不出
    这里要放图，出件时漏图是直接的形式评审风险。
    """
    from docx.enum.table import WD_ROW_HEIGHT_RULE
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    title, size_tip = slot_lines(image_slot_hint(line))
    slot = doc.add_table(rows=1, cols=1)
    try:
        slot.style = "Table Grid"
    except Exception:  # noqa: BLE001 - 模板缺样式时仍保留 1×1 结构
        pass
    _image_slot_borders(slot)
    row = slot.rows[0]
    row.height = Cm(4.5)
    try:
        row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    except Exception:  # noqa: BLE001
        pass
    cell = slot.cell(0, 0)
    cell.text = ""
    para = cell.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run(title)
    try:
        run.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
        run.font.bold = True
        run.font.size = Pt(10.5)
    except Exception:  # noqa: BLE001
        pass
    tip = cell.add_paragraph()
    tip.alignment = WD_ALIGN_PARAGRAPH.CENTER
    trun = tip.add_run(size_tip)
    try:
        trun.font.color.rgb = RGBColor(0xFF, 0x00, 0x00)
        trun.font.size = Pt(9)
    except Exception:  # noqa: BLE001
        pass
    note = cell.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    nrun = note.add_run("（此处插入图片：选中本框后「插入→图片」或直接粘贴；插图后可保留或删除边框）")
    try:
        nrun.font.color.rgb = RGBColor(0xA6, 0xA6, 0xA6)
        nrun.font.size = Pt(9)
    except Exception:  # noqa: BLE001
        pass
    doc.add_paragraph()


def _md_split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells = [c.strip() for c in s.split("|")]
    # Some extractors emit an empty covered-cell marker as literal HTML.
    # Preserve the column slot, rather than printing markup or deleting a cell.
    return ["" if re.fullmatch(r"<td\s+colspan\s*=\s*['\"]?\d+['\"]?\s*/\s*>", c, re.I) else c for c in cells]


def _md_disp_width(s: str) -> int:
    """文本显示宽度：中日韩字符与全角符号按 2 计，其余按 1 计。"""
    import unicodedata
    try:
        return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in (s or ""))
    except Exception:  # noqa: BLE001 - 个别代理对字符异常时退回字符数
        return len(s or "")


_MD_NUM_CELL = re.compile(r"^[¥￥$]?\s*-?[\d,，.]+(?:\s*(?:%|分|元|万元|亿元|天|个月|年|台|套|件|项|人|次|页|章))?$")


def _md_is_short_value(s: str) -> bool:
    """是否是「短数值」单元格（分值 / 金额 / 数量 / 比例 / 期限）——这类列居中更好读。"""
    return bool(_MD_NUM_CELL.match((s or "").strip()))


def _md_normalize_rows(rows: list[list[str]]) -> list[list[str]]:
    """把表格数据行对齐到表头列数。

    模型偶尔把 7 个字段塞进 4 列的表（如「环节 | 责任人 | 时间节点 | 记录与判据」下面的
    数据行多出 3 个格子）。若按「最大列数」建表，右侧会多出 3 个空表头列，导出后像表格错位。
    多出来的单元格并进最后一列（不丢内容），少的补空。
    """
    if not rows:
        return rows
    width = len(rows[0])
    normalized: list[list[str]] = []
    for row in rows:
        if len(row) > width:
            tail = "；".join(cell for cell in row[width - 1:] if cell)
            normalized.append(row[:width - 1] + [tail])
        elif len(row) < width:
            normalized.append(row + [""] * (width - len(row)))
        else:
            normalized.append(row)
    return normalized


def _md_add_table(doc, block: list[str]) -> None:
    """把 Markdown 表格块转成真正的 Word 表格。

    此前只设边框与表头底纹，python-docx 会把可排印宽度平均分给每一列，
    导致「一句话的说明列」和「一个数字的序号列」一样宽、且全部左对齐，非常难读。
    现在：列宽按各列内容的显示宽度加权分配（并设上下限），短数值列居中、
    表头居中、其余左对齐，单元格垂直居中并收紧行距，表头跨页自动重复。
    """
    from docx.enum.table import WD_ALIGN_VERTICAL
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement as _Ox
    from docx.oxml.ns import qn as _qn
    from docx.shared import Emu, Pt
    rows = [_md_split_row(b) for b in block if not _MD_TBL_SEP.match(b)]
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return
    # 列数必须按表头对齐，否则右侧多出空表头列（见 _md_normalize_rows）。
    rows = _md_normalize_rows(rows)
    ncol = len(rows[0])
    tbl = doc.add_table(rows=len(rows), cols=ncol)
    try:
        tbl.style = "Table Grid"  # 全框线
    except Exception:  # noqa: BLE001 - 模板缺样式时退化为无框线表格，不影响内容
        pass

    # ---- 列宽：按内容显示宽度加权 ----
    weights: list[float] = []
    centered: list[bool] = []
    for j in range(ncol):
        vals = [(r[j] if j < len(r) else "") for r in rows]
        widest = max((_md_disp_width(v) for v in vals), default=4)
        # 限制单列权重：太窄看不出层级，太宽会把其它列压瘪
        weights.append(float(min(max(widest, 6), 44)))
        body = [v.strip() for v in vals[1:] if v.strip()]
        centered.append(bool(body) and sum(1 for v in body if _md_is_short_value(v)) >= max(1, len(body) * 0.6))
    total_w = sum(weights) or 1.0
    try:
        # 正文在最后一节：封面那一节页边距是 0，拿它算会把表格按整张纸宽铺开
        sec = doc.sections[-1]
        avail = int(sec.page_width - sec.left_margin - sec.right_margin)
    except Exception:  # noqa: BLE001
        avail = 9144000  # 拿不到页面尺寸时退回约 16 厘米
    floor = int(avail * 0.05)
    widths = [max(int(avail * w / total_w), floor) for w in weights]
    scale = avail / float(sum(widths) or 1)
    widths = [int(w * scale) for w in widths]
    widths[-1] += avail - sum(widths)  # 补足到正好铺满可排印宽度

    # 固定布局：否则 Word 打开后会按内容重新分配列宽，白设
    try:
        tbl.autofit = False
        tblPr = tbl._tbl.tblPr
        lay = _Ox("w:tblLayout")
        lay.set(_qn("w:type"), "fixed")
        tblPr.append(lay)
        tw = _Ox("w:tblW")
        tw.set(_qn("w:w"), str(max(1, avail // 635)))  # EMU → twip
        tw.set(_qn("w:type"), "dxa")
        tblPr.append(tw)
        for j, w in enumerate(widths):
            tbl.columns[j].width = Emu(w)
    except Exception:  # noqa: BLE001 - 设不了固定布局也不该影响出件
        pass

    for i, r in enumerate(rows):
        for j in range(ncol):
            cell = tbl.cell(i, j)
            cell.text = ""
            try:
                cell.width = Emu(widths[j])
            except Exception:  # noqa: BLE001
                pass
            p = cell.paragraphs[0]
            _md_add_runs(p, r[j] if j < len(r) else "")
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if (i == 0 or centered[j]) else WD_ALIGN_PARAGRAPH.LEFT
            try:
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                p.paragraph_format.space_before = Pt(1.5)
                p.paragraph_format.space_after = Pt(1.5)
            except Exception:  # noqa: BLE001
                pass
            if i == 0:  # 表头加粗 + 浅灰底纹
                for run in p.runs:
                    run.bold = True
                shd = _Ox("w:shd")
                shd.set(_qn("w:val"), "clear")
                shd.set(_qn("w:fill"), "F2F4F8")
                cell._tc.get_or_add_tcPr().append(shd)
    # 表头跨页重复
    try:
        trPr = tbl.rows[0]._tr.get_or_add_trPr()
        th = _Ox("w:tblHeader")
        th.set(_qn("w:val"), "true")
        trPr.append(th)
    except Exception:  # noqa: BLE001
        pass
    doc.add_paragraph()  # 表格后留一个空段，避免与下文粘连


# ── 目录（TOC）：域 + 缓存条目 ───────────────────────────────────────────────
# 旧实现把一个空的 `TOC` 域塞进文档（还把 `w:p` 挂到 `w:fldSimple` 里面 —— OOXML 里
# fldSimple 只允许 run/hyperlink，Word 会把这些段落丢掉），所以打开是空白目录，必须
# 右键「更新域」才出内容。现在按 Word 自己保存目录的方式写：
#   TOC 域（begin/instrText/separate … end）+ 每条一章段落 + 书签锚点 + PAGEREF 页码域。
# 条目直接可见、点击可跳转，按 F9 更新域后由 Word 重算，结果与预填内容一致。
_TOC_MAX_LEVEL = 3            # 目录只收 Heading 1~3；Heading 4 不进目录
_TOC_PAGE_PLACEHOLDER = "—"   # 导出时算不出可靠页码：用中性占位，绝不编造页码


def _toc_level_cap(toc_level: Any) -> int:
    """目录层级上限：UI 传 1~4，但目录只做 Heading 1~3（Heading 4 不进目录）。"""
    try:
        value = int(toc_level)
    except (TypeError, ValueError):
        value = _TOC_MAX_LEVEL
    return max(1, min(value, _TOC_MAX_LEVEL))


def _toc_entries(entries: Any, cap: int) -> list[tuple[int, str, str]]:
    """过滤出真正要写进目录的条目：只要 1~cap 级、标题非空、带书签锚点。"""
    out: list[tuple[int, str, str]] = []
    for item in entries or []:
        try:
            lvl, text, bookmark = item
            lvl = int(lvl)
        except (TypeError, ValueError):
            continue
        text = str(text or "").strip()
        if not text or lvl < 1 or lvl > cap:
            continue
        out.append((lvl, text, str(bookmark or "")))
    return out


def _toc_paragraphs(entries: list[tuple[int, str, str]], cap: int,
                    tab_pos: int) -> list[Any]:
    """生成目录域与缓存条目段落。

    结构同 Word 自己保存的目录：第一段的开头是 `TOC` 的 begin/instrText/separate，
    每段一个 `w:hyperlink w:anchor` 指向正文标题的书签，段末是 PAGEREF 页码域
    （缓存结果是中性占位「—」），最后一段末尾补 field end。
    """
    # 本项目 OxmlElement/qn 一律在函数内导入（与其它 docx 辅助函数保持一致），模块级没有导入
    from docx.oxml import OxmlElement as _Ox
    from docx.oxml.ns import qn as _Qn

    def _run() -> Any:
        return _Ox("w:r")

    def _text_run(text: str) -> Any:
        run = _run()
        node = _Ox("w:t")
        node.set(_Qn("xml:space"), "preserve")
        node.text = str(text)
        run.append(node)
        return run

    def _fld_char(kind: str) -> Any:
        run = _run()
        node = _Ox("w:fldChar")
        node.set(_Qn("w:fldCharType"), kind)
        run.append(node)
        return run

    def _instr(code: str) -> Any:
        run = _run()
        node = _Ox("w:instrText")
        node.set(_Qn("xml:space"), "preserve")
        node.text = code
        run.append(node)
        return run

    head = [_fld_char("begin"),
            _instr(' TOC \\o "1-%d" \\h \\z \\u ' % int(cap)),
            _fld_char("separate")]
    if not entries:
        # 没有任何标题时也要留一个完整的域（begin/separate/end），
        # 不能留裸域：否则用户在 Word 里更新域后又是一片空白。
        para = _Ox("w:p")
        for node in head:
            para.append(node)
        para.append(_fld_char("end"))
        return [para]

    out: list[Any] = []
    last = len(entries) - 1
    for index, (lvl, text, bookmark) in enumerate(entries):
        para = _Ox("w:p")
        ppr = _Ox("w:pPr")
        tabs = _Ox("w:tabs")
        tab = _Ox("w:tab")
        tab.set(_Qn("w:val"), "right")
        tab.set(_Qn("w:leader"), "dot")   # 点线引导，和正式目录一致
        tab.set(_Qn("w:pos"), str(int(tab_pos)))
        tabs.append(tab)
        ppr.append(tabs)
        if lvl > 1:
            ind = _Ox("w:ind")
            ind.set(_Qn("w:left"), str(240 * (lvl - 1)))
            ind.set(_Qn("w:firstLine"), "0")
            ppr.append(ind)
        para.append(ppr)
        if index == 0:
            for node in head:
                para.append(node)
        link = _Ox("w:hyperlink")
        link.set(_Qn("w:anchor"), bookmark)   # 点击跳到正文里同名标题的书签
        link.set(_Qn("w:history"), "1")
        link.append(_text_run(text))
        tab_run = _run()
        tab_run.append(_Ox("w:tab"))
        link.append(tab_run)
        link.append(_fld_char("begin"))
        link.append(_instr(" PAGEREF %s \\h " % bookmark))
        link.append(_fld_char("separate"))
        link.append(_text_run(_TOC_PAGE_PLACEHOLDER))  # 不编造页码，更新域后才是真页码
        link.append(_fld_char("end"))
        para.append(link)
        if index == last:
            para.append(_fld_char("end"))
        out.append(para)
    return out


def _toc_tab_pos(doc: Any) -> int:
    """页码制表位位置：正文可用宽度（twip），右对齐到右边距。"""
    try:
        # 目录与正文同属最后一节；第一节是封面（页边距 0），不能拿来算
        sec = doc.sections[-1]
        avail = int(sec.page_width - sec.left_margin - sec.right_margin)
    except Exception:  # noqa: BLE001 - 拿不到页面尺寸时退回约 16 厘米
        avail = 9144000
    return max(1440, int(avail / 635))  # EMU → twip


def _toc_real_pages(blob: bytes) -> list[str]:
    """把成稿渲染一遍，返回每一页的文本（已去掉空白，便于按标题反查页码）。

    目录里写"—"占位是自欺欺人：客户打开标书就该看到页码，不该被要求按 F9 更新域。
    这里用一次无头渲染拿到每页文字，再按「标题出现在第几页」把页码填回目录。
    """
    import re as _re

    from pypdf import PdfReader

    pdf = convert_bytes_soffice(blob, "docx", "pdf")
    reader = PdfReader(io.BytesIO(pdf))
    pages = [_re.sub(r"\s+", "", page.extract_text() or "") for page in reader.pages]
    if not pages:
        return []
    return pages


def _fill_toc_page_numbers(blob: bytes) -> bytes:
    """把目录条目里的占位符换成真实页码；拿不到页码的行保持原样。"""
    import io as _io
    import re as _re

    import docx as _docx

    document = _docx.Document(_io.BytesIO(blob))
    entries: list[tuple[Any, str]] = []      # (占位 run, 标题)
    for paragraph in document.element.body.iter(qn("w:p")):
        instr = "".join(t.text or "" for t in paragraph.iter(qn("w:instrText")))
        if "PAGEREF" not in instr:
            continue
        link = paragraph.find(qn("w:hyperlink"))
        title = "".join(t.text or "" for t in link.iter(qn("w:t"))) if link is not None else ""
        # 占位符本身也在 hyperlink 里，不剥掉的话标题变成「一、投标函—」，正文里永远找不到
        title = title.replace(_TOC_PAGE_PLACEHOLDER, "").strip()
        placeholder = None
        for run in paragraph.iter(qn("w:r")):
            text = "".join(t.text or "" for t in run.iter(qn("w:t")))
            if text == _TOC_PAGE_PLACEHOLDER:
                placeholder = run
                break
        if placeholder is not None and title:
            entries.append((placeholder, title))
    if not entries:
        return blob
    pages = _toc_real_pages(blob)
    if not pages:
        return blob
    titles = sorted({"".join(t.split()) for _r, t in entries if t and t.strip()},
                    key=len, reverse=True)

    def _toc_lines(text: str) -> int:
        """这一页里有几行「标题 + 点线引导」—— 目录行独有的结构指纹。

        只看「一页出现 ≥3 个标题」两头都会翻车：三个短章挤在同一页的**正文页**会被
        误判成目录页（这一页被整页跳过，章页码退回占位符）；只有一两条的**目录页**
        又判不出来（所有条目都填成目录页的页码）。点线只有目录才有：正文页排得再密
        也不会出现「标题……数字」这种行。
        """
        count = 0
        for title in titles:
            if _re.search(_re.escape(title) + r"[.．·…]{3,}", text):
                count += 1
        return count

    lines_per_page = [_toc_lines(text) for text in pages]
    # 渲染器把点线吞掉时（个别 LibreOffice 版本），只能退回旧的「标题个数」口径
    leaders_rendered = any(lines_per_page)

    def _is_toc_page(index: int) -> bool:
        text = pages[index]
        lines = lines_per_page[index]
        if lines >= 2:                        # 一页两行以上点线：就是目录
            return True
        if lines >= 1 and "目录" in text:      # 目录只有一两条时靠「目录」标题认
            return True
        if leaders_rendered:                   # 点线渲染得出来 → 正文页不再按标题个数判
            return False
        return sum(1 for title in titles if title and title in text) >= 3
    body_pages = [i for i in range(len(pages)) if not _is_toc_page(i)]
    if not body_pages:
        return blob
    # 正文页按出现顺序找：页码单调递增，找到就往后走，避免同名小标题串页
    cursor = body_pages[0]
    filled = 0
    for run, title in entries:
        key = "".join(title.split())
        target = None
        for index in body_pages:
            if index < cursor:
                continue
            if key and key in pages[index]:
                target = index
                break
        if target is None:
            continue
        cursor = target
        node = run.find(qn("w:t"))
        if node is None:
            continue
        node.text = str(target + 1)
        node.set(qn("xml:space"), "preserve")
        filled += 1
    if not filled:
        return blob
    buf = _io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _add_page_field_run(paragraph: Any, code: str) -> None:
    """往段落末尾追加一个 PAGE / NUMPAGES 域（begin + instrText + end）。"""
    from docx.oxml import OxmlElement

    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = str(code)
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, end):
        run._r.append(node)


def _footer_has_page_field(part: Any) -> bool:
    """页脚里是否已经有页码域（模板自带的「第 X 页」不再重复加一个）。"""
    for node in part.element.iter(qn("w:instrText")):
        code = "".join(str(node.text or "").split()).upper()
        if not code or code.startswith("PAGEREF"):
            continue                       # PAGEREF 是目录里的交叉引用，不是页脚页码
        if code.startswith("PAGE") or "NUMPAGES" in code:
            return True
    return False


def _line_spacing_pt(value: Any) -> float | None:
    """配置里的行距值 → 磅数。

    历史配置把 520（twips，= 26 磅）填了进来，旧实现当磅读再被夹到 72，
    导致正文被套 72 磅固定行距、每页只排 9 行。这里按量级判单位：
    twips 的合理区间是 160~800（8~40 磅），磅的合理区间是 8~40，两段不重叠，
    所以 **≥160 才当 twips 折算**，其余仍按磅——用户真填 22/72 想当磅也不会被除错。
    """
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    pt = raw / 20.0 if raw >= 160 else raw
    return max(8.0, min(pt, 40.0))


def _is_section_marker(el: Any) -> bool:
    """是不是 _push_section 插的那根「1pt 分节空段」。

    它只为携带 sectPr 而存在，文字是空的；一旦被套上正文行距（曾经是 72 磅），
    就凭空占掉大半页——每换一次节就多一页半空白。
    """
    try:
        ppr = el.find(qn("w:pPr"))
        if ppr is None or ppr.find(qn("w:sectPr")) is None:
            return False
        rpr = ppr.find(qn("w:rPr"))
        if rpr is None:
            return True
        sz = rpr.find(qn("w:sz"))
        if sz is None:
            return True
        return str(sz.get(qn("w:val")) or "") in ("2", "1")
    except Exception:  # noqa: BLE001
        return False


def _is_heading(el: Any) -> bool:
    """是不是标题段（Heading1~4 / Title / TOCHeading）。

    标题的行高由标题自己的样式决定，套上正文字距只会把标题撑成一页。
    """
    try:
        ppr = el.find(qn("w:pPr"))
        if ppr is None:
            return False
        style = ppr.find(qn("w:pStyle"))
        if style is None:
            return False
        sid = str(style.get(qn("w:val")) or "")
        if sid in ("Title", "TOCHeading"):
            return True
        if sid.startswith("Heading") and sid[7:].isdigit():
            return 1 <= int(sid[7:]) <= 4
        return False
    except Exception:  # noqa: BLE001
        return False


def _has_drawing(el: Any) -> bool:
    """段落里有没有图（整页页图或插图）。

    固定行距会把「行高」压到设定值，图又不会自己缩——所以给带图的段落套
    `lineRule=exact` 等于把图裁掉大半。PDF 原件章就是靠这个变成半页空白。
    """
    try:
        return (el.find(".//" + qn("w:drawing")) is not None
                or el.find(".//" + qn("w:pict")) is not None)
    except Exception:  # noqa: BLE001
        return False


def _is_pdf_page_section(section: Any) -> bool:
    """PDF 原件章所在的节：按原页铺满、四边页边距与页眉页脚距离**同时**为 0。

    （见 `_ensure_section` 的 pdf 分支：`_pdf_geom` 把六个值一起归零。）

    为什么连页眉页脚距离也要比：用户导出时可以把页边距设成 0（四边都是 0），
    但页眉页脚距离仍是默认 12.7mm。只看四边，整本标书都会被误判成 PDF 章，
    页眉和页码会被全局关掉——那比"没关"严重得多。
    """
    try:
        return all(int(getattr(section, name) or 0) == 0 for name in
                   ("left_margin", "right_margin", "top_margin", "bottom_margin",
                    "header_distance", "footer_distance"))
    except Exception:  # noqa: BLE001
        return False


def _detach_furniture(section: Any) -> None:
    """断开本节与上一节的页眉页脚继承，让它用自己的空定义（不写内容）。"""
    for name in ("header", "footer"):
        try:
            part = getattr(section, name)
            if part.is_linked_to_previous:
                part.is_linked_to_previous = False
        except Exception:  # noqa: BLE001
            pass


def _own_footer(section: Any) -> Any:
    """拿本节自己的页脚部件：没有就先断开继承，断不开返回 None。"""
    try:
        footer = section.footer
        if footer.is_linked_to_previous:
            footer.is_linked_to_previous = False
            footer = section.footer
        return None if footer.is_linked_to_previous else footer
    except Exception:  # noqa: BLE001
        return None


def _write_page_footer(doc: Any) -> None:
    """页脚写「第 X 页 共 Y 页」：PAGE 域 + NUMPAGES 域，每个页脚定义只写一次。

    线上事故：成稿每一页的页脚是「111」「222」…「646464」——三个节共用同一个
    footer 部件（第 2、3 节没有 footerReference，按 OOXML 继承第 1 节的），旧实现按
    `doc.sections` 逐个 `footer.paragraphs[0].add_run()`，把 3 个 PAGE 域叠进了同一段，
    页脚就把同一个页码连打三遍；NUMPAGES 和「第…页 共…页」这段文字压根没写过。
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    targets: list[tuple[Any, Any]] = []
    seen: set[int] = set()
    sections = list(doc.sections)
    for idx, section in enumerate(sections):
        if _is_pdf_page_section(section):
            # 原页里已经印着招标文件自己的页脚页码；再叠一层平台页码就成两行数字
            _detach_furniture(section)
            continue
        footer = section.footer
        if footer.is_linked_to_previous:
            prev_pdf = idx > 0 and _is_pdf_page_section(sections[idx - 1])
            if not prev_pdf:
                continue                   # 本节继承上一节的页脚：写定义它的那节就够了
            # 上一节是 PDF 原页节：它的页脚是空定义，继承下去等于把页码写没了
            # （实测反了：封面那节没页码，反倒 PDF 节带着 PAGE 域）。
            footer = _own_footer(section)
            if footer is None:
                continue
        part = footer.part                 # 多节共用一个 footer 部件时拿到的是同一个对象
        if id(part) in seen:
            continue
        seen.add(id(part))
        targets.append((footer, part))
    if not targets and doc.sections:
        # 整篇没有页脚定义（全新文档）：给第一节建一个，后面各节照样继承
        footer = doc.sections[0].footer
        targets.append((footer, footer.part))
    for footer, part in targets:
        if _footer_has_page_field(part):
            continue                       # 模板自带页码：不去动它的样式
        paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.add_run("第 ")
        _add_page_field_run(paragraph, "PAGE")
        paragraph.add_run(" 页 共 ")
        _add_page_field_run(paragraph, "NUMPAGES")
        paragraph.add_run(" 页")


def _add_toc_field(doc: Any, anchor_p: Any, entries: Any, level: int = 3) -> int:
    """把「TOC 域 + 缓存条目」插到 anchor 之前并移除 anchor，返回写入的条目数。"""
    cap = _toc_level_cap(level)
    kept = _toc_entries(entries, cap)
    for para in _toc_paragraphs(kept, cap, _toc_tab_pos(doc)):
        anchor_p.addprevious(para)
    parent = anchor_p.getparent()
    if parent is not None:
        parent.remove(anchor_p)
    return len(kept)


def _add_bookmark(paragraph: Any, name: str, bookmark_id: int) -> None:
    """给标题段落挂书签：目录条目的 `w:hyperlink w:anchor` 指向它。"""
    from docx.oxml import OxmlElement as _Ox
    from docx.oxml.ns import qn as _Qn

    node = paragraph._p
    start = _Ox("w:bookmarkStart")
    start.set(_Qn("w:id"), str(int(bookmark_id)))
    start.set(_Qn("w:name"), name)
    end = _Ox("w:bookmarkEnd")
    end.set(_Qn("w:id"), str(int(bookmark_id)))
    ppr = node.find(_Qn("w:pPr"))
    if ppr is not None:
        ppr.addnext(start)
    else:
        node.insert(0, start)
    node.append(end)


def _exclude_from_toc(paragraph: Any) -> None:
    """「目录」标题自己不进目录：显式标成正文级别，Word 更新域时同样跳过它。"""
    from docx.oxml import OxmlElement as _Ox
    from docx.oxml.ns import qn as _Qn

    ppr = paragraph._p.get_or_add_pPr()
    old = ppr.find(_Qn("w:outlineLvl"))
    if old is not None:
        ppr.remove(old)
    lvl = _Ox("w:outlineLvl")
    lvl.set(_Qn("w:val"), "9")  # 9 = body text（不进大纲/目录）
    ppr.append(lvl)


def _add_toc_title(doc: Any, text: str = "目录") -> Any:
    """「目录」标题用 Word 自己的 TOC Heading 样式（基于 Heading 1，大纲级别=正文）。

    这样它长得和一级标题一样，但 Word 按 F9 更新目录时不会把「目录」自己也列进去；
    模板里没有这个样式时退回「Heading 1 + 正文大纲级别」。
    """
    try:
        paragraph = doc.add_paragraph(text, style="TOC Heading")
        if str(paragraph.style.name or "") == "TOC Heading":
            return paragraph
    except (KeyError, ValueError):
        pass
    paragraph = doc.add_heading(text, level=1)
    _exclude_from_toc(paragraph)
    return paragraph


class _TocCollector:
    """同一次导出里唯一的标题清单。

    正文每写一个 Heading 1~3 就登记一条并给它挂书签；目录条目只按这份清单生成，
    不另算一遍，所以永远与正文标题一一对应。空标题（如只剩编号的「4.7」章）与
    Heading 4 都不登记。
    """

    __slots__ = ("entries", "_seq")

    def __init__(self) -> None:
        self.entries: list[tuple[int, str, str]] = []
        self._seq = 0

    @staticmethod
    def _next_bookmark_id(paragraph: Any) -> int:
        """在**全文**范围内取一个没被占用的 `w:bookmarkStart/@w:id`。

        `w:id` 在文档里是书签的主键，必须全局唯一。但成稿的书签有两个来源：
        平台自己挂的 `_Toc…`（本类分配）和**从招标件切片原样搬进来的**
        `__RefHeading___Toc*`、`_Ref…` 等（`format_template.append_to_document`）。
        如果这里还拿 `self._seq` 直接当 id，第 17 章就会和模板搬入的 `id=17` 撞车——
        实测真实导出件里正是 `_Toc000000017` 与 `__RefHeading___Toc4969` 抢同一个 id。

        撞车不会让 Word 报损坏（实测容忍），但 Word 以 id 定位书签：目录跳转与
        `REF` 域会取到另一个标题，属于「点了目录跳错页」这类难查的问题。
        所以每次分配前都对当前整棵树取 max+1 —— 模板章是在导出中途插进来的，
        不能只在开头算一次。
        """
        try:
            from docx.oxml.ns import qn as _qn

            root = paragraph._p
            while root.getparent() is not None:
                root = root.getparent()
            used = []
            for bookmark in root.iter(_qn("w:bookmarkStart")):
                raw = bookmark.get(_qn("w:id"))
                if raw and str(raw).lstrip("-").isdigit():
                    used.append(int(raw))
            return max(used) + 1 if used else 1
        except Exception:  # noqa: BLE001 —— 取不到就退回序号，绝不能让导出中断
            return 0

    def add(self, paragraph: Any, text: str, level: Any) -> None:
        title = str(text or "").strip()
        try:
            lvl = int(level)
        except (TypeError, ValueError):
            return
        if not title or lvl < 1 or lvl > _TOC_MAX_LEVEL:
            return
        self._seq += 1
        name = "_Toc%09d" % self._seq          # 名字仍按序号（模板搬入的名字不同，不撞）
        bookmark_id = self._next_bookmark_id(paragraph)
        _add_bookmark(paragraph, name, bookmark_id if bookmark_id else self._seq)
        self.entries.append((lvl, title, name))


def _add_export_heading(doc: Any, text: str, level: int, *, toc: Any = None,
                        record: bool = True) -> Any:
    """写一个正文标题；传了 collector 就顺手登记进目录清单（并挂书签）。"""
    paragraph = doc.add_heading(text, level=level)
    if level == 1:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if toc is not None and record:
        toc.add(paragraph, text, level)
    return paragraph


def _row_title(row: Any) -> str:
    """章节标题去掉孤立引号（「8.1 零整比”」）：目录与正文标题共用同一个清洗。"""
    from zhibiao.core.text_hygiene import fix_quote_pairing
    return fix_quote_pairing(str(row.get("title") or ""))


def _add_cover(doc, *, dims: list[dict[str, Any]], project_name: str, scope_title: str,
               cover_plan: dict[str, Any] | None = None,
               cover_lines: list[dict[str, Any]] | None = None,
               cover_style: dict[str, Any] | None = None,
               profile_facts: dict[str, str] | None = None) -> str:
    """Write exactly one request-local cover, followed by one section boundary."""
    from zhibiao.core.tender_parse import clean_project_name, clean_value, value_usable
    from zhibiao.core.cover_from_pdf import (build_cover_plan, default_cover_plan,
                                         finish_cover_section, fit_word_cover_one_page)

    def fact(name: str) -> str:
        row = next((d for d in dims or [] if d.get("name") == name and d.get("hit")), None)
        if not row:
            return ""
        value = (clean_project_name(row.get("value")) if name in ("项目名称", "招标项目名称")
                 else clean_value(row.get("value")))
        return value if value_usable(name, value) else ""

    official = fact("项目名称") or fact("招标项目名称")
    title = official or "{{待填:招标项目名称}}"
    known = {key: fact(key) for key in ("招标编号", "项目编号", "招标人", "招标代理机构")}
    slots = {**known, **(profile_facts or {})}
    # 「（项目名称）」是招标原件自己留的填空。只有真读到这份招标文件的项目名称才填；
    # 平台里的项目名是用户随手起的名字（「测试用例2」），塞进去就成了
    # 「测试用例2设备采购招标项目」这种原文里根本没有的句子，所以留红字让人填。
    if official:
        slots.update({"项目名称": official, "（项目名称）": official,
                      "(项目名称)": official, "{{待填:项目名称}}": official})
    for label, value in {**known, **(profile_facts or {})}.items():
        if value:
            slots["{{待填:" + label + "}}"] = str(value)
            slots["（" + label + "）"] = str(value)
    plan = cover_plan or default_cover_plan(title, known, cover_style)
    if plan.get("kind") in ("style", "word-style"):
        source_style = plan.get("style") or plan
        styled = default_cover_plan(title, known, source_style)
        geometry = plan.get("geometry") or {}
        for axis, name in (("width", "page_width"), ("height", "page_height")):
            if geometry.get(name):
                styled[axis] = float(geometry[name]) / 12700
        plan = styled
    elements = build_cover_plan(doc, plan, slots)
    finish_cover_section(doc, plan)
    if plan.get("kind") == "word":
        # A long official project name wraps; the template's blank lines absorb
        # that so the cover never spills onto a second page.
        fit_word_cover_one_page(doc, elements)
    return title


def _ensure_bid_letter_salutation(doc: Any) -> None:
    """投标函（本章各版都叫「投标函」）开头必须有「致：<招标人>」。

    实测：1023——pdf02 的 1.1 投标函全文没有「致：」，形式上缺要件，
    严格评审会要求澄清。招标人名称从封面「招标人：…」那一行取。
    """
    import re as _re

    agent = ""
    for para in doc.paragraphs:
        text = str(para.text or "").strip()
        match = _re.match(r"招标人\s*[:：]\s*(.+)$", text)
        if match:
            agent = match.group(1).strip()
            break
    if not agent:
        return
    paragraphs = doc.paragraphs
    for index, para in enumerate(paragraphs):
        text = str(para.text or "").strip()
        if not text.endswith("投标函") or len(text) > 30:
            continue
        following = [str(p.text or "").strip() for p in paragraphs[index + 1:index + 6]]
        if any(t.startswith("致") for t in following):
            return
        new_p = paragraphs[index].insert_paragraph_before("致：" + agent)
        new_p.style = paragraphs[index + 1].style if index + 1 < len(paragraphs) else new_p.style
        return

_AUTOFILL_RULES = (
    ("招标代理", r"招标代理机构\s*[:：]\s*(\S+)"),
    ("招标人", r"^招标人\s*[:：]\s*(\S+)"),
    ("招标编号", r"招标(?:文件)?编号\s*[:：]\s*([A-Za-z0-9._/\-]+)"),
    ("项目编号", r"(?:招标项目编号|(?<!招标)项目编号)\s*[:：]\s*([A-Za-z0-9._/\-]+)"),
    ("投标保证金", r"投标保证金[^\n]{0,24}?([0-9][0-9,\.]*\s*(?:万)?元)"),
    ("服务期", r"(?:服务期|交货期|工期)[^\n]{0,12}?(\d+\s*(?:日历)?[天日])"),
    ("投标有效期", r"投标有效期[^\n]{0,12}?(\d+\s*[天日])"),
)


def _autofill_known_fields(doc: Any, source_facts: dict[str, str] | None = None) -> None:
    """Fill only exact, allowlisted identities from this request's source facts.

    Never mine generated prose as evidence or substring-match historical fields.
    Facts belong to this document, not shared mutable process state.
    """
    import re as _re

    facts = {str(k): str(v) for k, v in (source_facts or {}).items()
             if v and "待填" not in str(v)}
    if "项目名称" not in facts:
        cover = _official_name_from_doc(doc)
        if len(cover) >= 8 and "待填" not in cover:
            facts["项目名称"] = cover
    if not facts:
        return

    dark = "8B0000"
    holder = _re.compile(r"\{\{待填[:：]([^}]{1,40})\}\}")
    aliases = {
        "项目名称": ("项目名称", "招标项目名称", "本项目名称"),
        "招标人": ("招标人", "招标人名称"),
        "招标代理": ("招标代理", "招标代理机构", "招标代理机构名称"),
        "招标编号": ("招标编号",), "项目编号": ("项目编号",),
        "投标保证金": ("投标保证金", "投标保证金金额"),
        "服务期": ("服务期", "服务期限"), "交货期": ("交货期", "工期"),
        "投标有效期": ("投标有效期", "投标有效期限"),
        "投标人名称": ("投标人名称", "投标人全称", "投标单位名称"),
        "法定代表人": ("法定代表人", "法定代表人姓名"),
        "统一社会信用代码": ("统一社会信用代码",),
        "开户银行": ("开户银行", "开户行"), "银行账号": ("银行账号",),
    }
    exact = {alias: facts[key] for key, names in aliases.items() if key in facts for alias in names}

    def rewrite(para) -> bool:
        changed = False
        runs=list(para.runs)
        joined=''.join(run.text for run in runs)
        spans=[];offset=0
        for run in runs:
            spans.append((offset,offset+len(run.text),run));offset+=len(run.text)
        # Word commonly splits a placeholder across formatting/spellcheck runs.
        # Work right-to-left and preserve every run outside the matched field.
        for match in reversed(list(holder.finditer(joined))):
            if match.group(1) not in exact:continue
            touched=[(start,end,run) for start,end,run in spans
                     if end>match.start() and start<match.end()]
            if not touched:continue
            start,_,first=touched[0];last_start,_,last=touched[-1]
            prefix=first.text[:match.start()-start]
            suffix=last.text[match.end()-last_start:]
            first.text=prefix+exact[match.group(1)]+(suffix if first is last else '')
            if first is not last:
                for _,_,run in touched[1:-1]:run.text=''
                last.text=suffix
            from docx.shared import RGBColor as _RGB
            first.font.color.rgb=_RGB(0x8B,0x00,0x00)
            changed=True
        return changed

    for para in doc.paragraphs:
        rewrite(para)
    for table in doc.tables:
        _table_text = " ".join(cell.text for row in table.rows for cell in row.cells)
        if "项目名称" in _table_text and re.search(r"业绩|历史|合同签订|合同金额|买方|客户|用户单位|业主单位", _table_text):
            # Even an underspecified {{待填:项目名称}} inside a historical record
            # belongs to that contract, not the currently tendered project.
            continue
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    rewrite(para)

def _official_name_from_doc(doc: Any) -> str:
    """官方项目名以封面标题为准（封面就是招标文件给的正式名称）。"""
    for para in doc.paragraphs[:12]:
        text = str(para.text or "").strip()
        if len(text) >= 8:
            return text
    return ""


def _canon_project_names(doc: Any) -> None:
    """把正文里同一项目名的变体（中间多一个「实施/建设/承建」）改回官方名称。

    实测：技术部分写成「XX 分公司实施 XX 项目」，与封面、投标函不一致；
    招标书第 25 页 20.3 条要求实质性响应，名称不符是形式性瑕疵。
    只替换「官方名 + 插入词」这一种模式，其余文字一字不动。
    """
    import re as _re

    name = _official_name_from_doc(doc)
    if len(name) < 8:
        return
    head, tail = name[:8], name[8:]
    if not tail:
        return
    pattern = _re.compile("|".join(
        _re.escape(head) + filler + _re.escape(tail) for filler in ("实施", "建设", "承建")))
    if not pattern.search("".join(str(p.text or "") for p in doc.paragraphs)):
        return

    def fix(text: str) -> str:
        return pattern.sub(name, text) if text else text

    for para in doc.paragraphs:
        if not para.text or not pattern.search(para.text):
            continue
        for run in para.runs:
            if run.text and pattern.search(run.text):
                run.text = fix(run.text)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        if run.text and pattern.search(run.text):
                            run.text = fix(run.text)

def _md_render_chapter(doc, content: str, base_level: int, title: str,
                       images: dict[str, bytes] | None = None, *,
                       chapter_number: str = "", container: bool = False,
                       toc: Any = None) -> None:
    """把一章的 Markdown 正文写入 docx。

    - #~######  → Word 标题（按正文最浅标题换算相对层级，保证目录层级正确）
    - **粗体**  → 加粗 run
    - `- `      → List Bullet 项目符号
    - `1. `     → 保留作者原编号的悬挂缩进段落（不交给 Word 重排，避免跨列表续号）
    - 表格       → 真表格
    - ![图](/api/images/<id>) → 真图片（从库里取字节嵌入，宽度按正文可用宽度等比缩放）
    - `---`     → 丢弃（Markdown 水平线在 Word 里没有对应物）
    """
    from docx.shared import Pt as _Pt
    from zhibiao.core.document_structure import normalize_export_structure

    # Remove title echoes before measuring relative depth, and promote only
    # unambiguous standalone dotted headings (never prose or table cells).
    normalized = normalize_export_structure(content, title, chapter_number, container=container)
    lines = normalized.split("\n")
    hmin = 2  # The pure normalizer emits contiguous relative headings starting at ##.
    base = max(1, min(int(base_level or 1), 4))
    fence = ""
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            i += 1
            continue
        if fence:
            doc.add_paragraph().add_run(line).font.name = "Consolas"
            i += 1
            continue
        if not line.strip():
            i += 1
            continue

        # 待补图片专用 1x1 可编辑 Word 表格；已有图片及普通文字不受影响。
        if is_image_slot_line(line):
            _md_add_image_slot(doc, line.strip())
            i += 1
            continue

        # 表格：把连续的 | 行整块取出来
        if _MD_TBL.match(line):
            blk: list[str] = []
            while i < len(lines) and _MD_TBL.match(lines[i].rstrip()):
                blk.append(lines[i].rstrip())
                i += 1
            _md_add_table(doc, blk)
            continue

        # 水平线：直接丢弃
        if _MD_HR.match(line):
            i += 1
            continue

        # 标题
        m = _MD_HEAD.match(line)
        if m:
            txt = m.group(2).strip()
            if txt and container:
                # 容器章（只有下级章节的概述章）的正文小标题不进目录、不占标题层级：
                # 旧实现把它们渲染成与下级章节同级的 Heading 2，目录里出现一串
                # 没有编号的「本章范围与响应原则 / 无偏离声明」，看着像目录乱序。
                _p = doc.add_paragraph()
                _run = _p.add_run(txt)
                _run.bold = True
                i += 1
                continue
            lvl = max(1, min(base + (len(m.group(1)) - hmin) + 1, 9))
            if txt:
                # 与章节标题走同一个出口：登记进目录清单（同一次导出只用这份清单）
                _add_export_heading(doc, txt, lvl, toc=toc)
            i += 1
            continue

        # 引用：去掉 > 后按普通段落渲染，左侧留缩进
        mq = _MD_QUOTE.match(line)
        if mq:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = _Pt(21)
            _md_add_runs(p, mq.group(1).strip())
            i += 1
            continue

        # 无序列表 → 真项目符号
        mu = _MD_UL.match(line)
        if mu:
            p = doc.add_paragraph(style="List Bullet")
            _md_add_runs(p, mu.group(2).strip())
            i += 1
            continue

        # 有序列表 → 保留原编号 + 悬挂缩进
        mo = _MD_OL.match(line)
        if mo:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = _Pt(21)
            p.paragraph_format.first_line_indent = _Pt(-21)
            p.add_run(mo.group(2) + ". ")
            _md_add_runs(p, mo.group(3).strip())
            i += 1
            continue

        # 图片：![说明](/api/images/<uuid>) —— 从库里取字节真正嵌入
        mi = _MD_IMG.match(line)
        if mi:
            iid = (mi.group(2) or "").rstrip("/").split("/")[-1]
            raw = (images or {}).get(iid)
            if raw:
                try:
                    from docx.enum.text import WD_ALIGN_PARAGRAPH
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.add_run().add_picture(io.BytesIO(raw), width=_md_img_width(doc))
                    if mi.group(1).strip():
                        cap = doc.add_paragraph()
                        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        _md_add_runs(cap, "图：" + mi.group(1).strip())
                except Exception as exc:  # noqa: BLE001 图片坏了不能拖垮整篇导出
                    # 不再静默吞掉：写进容器日志，便于排查「导出后图没了」
                    print("[export] 嵌入图片失败 image=%s: %s: %s" % (iid, type(exc).__name__, exc), flush=True)
                    p = doc.add_paragraph()
                    _md_add_runs(p, "［图片］" + (mi.group(1) or "") + "（无法嵌入，请在预览区重新插入）")
            else:
                p = doc.add_paragraph()
                _md_add_runs(p, "［图片］" + (mi.group(1) or "") + "（图片已不在库中，请在预览区重新插入）")
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        _md_add_runs(p, line.strip())
        i += 1


def _set_outline_level(paragraph_element: Any, level: int) -> None:
    """给段落标大纲级别（1→0 …），使模板章在 Word「更新目录域」后仍进目录。

    只写 `w:outlineLvl` 这一个属性：字体、字号、对齐、行距、缩进一概不碰，所以它不会
    改变这一页的任何可见排版——模板的标题仍然长模板的样子，只是"能被目录认出来"。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    try:
        ppr = paragraph_element.get_or_add_pPr()
    except AttributeError:
        return
    old = ppr.find(qn("w:outlineLvl"))
    if old is not None:
        ppr.remove(old)
    lvl = OxmlElement("w:outlineLvl")
    lvl.set(qn("w:val"), str(max(0, min(8, int(level) - 1))))
    ppr.append(lvl)


_GEOM_FIELDS = ("page_width", "page_height", "top_margin", "bottom_margin",
                "left_margin", "right_margin", "header_distance", "footer_distance")


def _geometry_key(setup: dict[str, Any] | None) -> tuple[int, ...]:
    """页面几何指纹：只比纸张和页边距，用来判断「要不要为这一章另起一节」。"""
    values = []
    for name in _GEOM_FIELDS:
        try:
            values.append(int((setup or {}).get(name) or 0))
        except (TypeError, ValueError):
            values.append(0)
    return tuple(values)


def _apply_geometry(section: Any, setup: dict[str, Any] | None) -> None:
    """把页面设置应用到某一节（只认页面几何，不碰页眉页脚与页码）。"""
    if not setup:
        return
    for name in _GEOM_FIELDS:
        value = setup.get(name)
        if value is None:
            continue
        try:
            setattr(section, name, int(value))   # 注意 0 有效：PDF 章节页边距就是 0
        except Exception:  # noqa: BLE001
            continue
    orientation = setup.get("orientation")
    if orientation is not None:
        try:
            from docx.enum.section import WD_ORIENT

            section.orientation = WD_ORIENT(int(orientation))
        except Exception:  # noqa: BLE001
            pass


def _push_section(doc: Any, setup: dict[str, Any] | None) -> None:
    """在这里切断一节：之前的内容保持当前页面设置，之后的正文改用 `setup`。

    为什么不直接用 `doc.add_section()`：它插入的分节段落是正常字号，紧跟在一页满图
    之后时会被挤到下一页，凭空多出一张白纸。这里自己插 1pt 空段，肉眼不可见。
    """
    from docx.oxml import OxmlElement

    body = doc.element.body
    current = body.find(qn("w:sectPr"))
    if current is None:
        return
    # 上一章本身就是以"分节符"结尾的（招标原件常这样分页），这里再切一刀就会多一张白纸
    tail = current.getprevious()
    if tail is not None and tail.tag == qn("w:p"):
        ppr = tail.find(qn("w:pPr"))
        if ppr is not None and ppr.find(qn("w:sectPr")) is not None:
            _apply_geometry(doc.sections[-1], setup)
            return
    para = OxmlElement("w:p")
    ppr = OxmlElement("w:pPr")
    rpr = OxmlElement("w:rPr")          # pPr 里 rPr 必须在 sectPr 之前
    size = OxmlElement("w:sz")
    size.set(qn("w:val"), "2")          # 半磅制：2 = 1pt
    rpr.append(size)
    ppr.append(rpr)
    # 行距也要定死：只把字号设成 1pt 时，行高仍按样式里的字号算（实测吃掉 20pt，
    # 紧跟其后的正文整体下移 20.3pt，页底还会被顶出一张空白页）。
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:line"), "20")
    spacing.set(qn("w:lineRule"), "exact")
    spacing.set(qn("w:before"), "0")
    spacing.set(qn("w:after"), "0")
    ppr.append(spacing)
    ppr.append(copy.deepcopy(current))  # 本节的页面设置封在这里
    para.append(ppr)
    current.addprevious(para)
    _apply_geometry(doc.sections[-1], setup)


def _adopt_paper(doc: Any, geom: dict[str, Any], platform: dict[str, Any]) -> dict[str, Any]:
    """整篇改用招标文件的纸张（A4/横向），页边距保持平台设置。

    成稿里一半 Letter 一半 A4 是最难看的：翻页时纸张跳变。招标文件用 A4，
    这份标书就应当是 A4；页边距是用户在导出时选的，保持不变。
    """
    merged = dict(platform)
    for name in ("page_width", "page_height", "orientation"):
        if geom.get(name):
            merged[name] = int(geom[name])
    for section in doc.sections:
        _apply_geometry(section, merged)
    return merged


def _strip_leading_toc(elements: list) -> list:
    """剪掉招标文件格式章开头那张「小节目录」。

    原件在这一章最前面常放一张自己的目录（「目录 / 一、投标函 …… 12 / 二、授权委托书 …… 15」），
    页码指的是招标文件本身。原样搬进标书就会和平台生成的目录并排出现两张目录，客户看到的
    就是"前面错乱"。这里按结构剪：开头若是「目录」标题或带右对齐制表位的目录行，一律丢掉，
    遇到第一个不带这种制表位的块（真正的小节标题）就停；结构不像目录则原样返回，绝不误删。
    返回 [(元素, 是否可删)] —— 调用方据此判断是否真的剪掉了东西。
    """
    if not elements:
        return elements

    def _text(el: Any) -> str:
        return "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()

    def _is_toc_line(el: Any) -> bool:
        if getattr(el, "tag", None) != qn("w:p"):
            return False
        ppr = el.find(qn("w:pPr"))
        if ppr is None:
            return False
        tabs = ppr.find(qn("w:tabs"))
        if tabs is None:
            return False
        return any(t.get(qn("w:val")) == "right" for t in tabs.findall(qn("w:tab")))

    head = _text(elements[0]).replace(" ", "").replace("\u3000", "")
    if head not in ("目录", "目次", "章节目录"):
        return elements
    index = 0
    while index < len(elements):
        element = elements[index]
        text = _text(element).replace(" ", "").replace("\u3000", "")
        if text in ("目录", "目次", "章节目录") or _is_toc_line(element) or not text:
            index += 1
            continue
        break
    # 剪到最后一个空块之后：标题前那一两个空段留着不影响，但不要留下半张目录
    kept = elements[index:]
    if not kept:
        return elements
    # 剪掉的块必须**从 XML 树上撤掉**，只 return 切片不够：`append_to_document` 已经把它们
    # 追进成稿了，留着就是两张目录；而且 `_force_page_break` 会挂到「第一块」= 被裁的目录行上，
    # 真正的章标题反倒不换页。写法与 `_append_template_chapter` 里的 `_drop()` 一致。
    for element in elements[:index]:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    return kept


def _is_removable_blank(element: Any) -> bool:
    """这一块是不是「纯凑页空段」——只有这种才允许从切片尾部剪掉。

    四道闸门（任一条不满足就留住），对应三种「不能误伤」：

    1. 有文字 → 不是空段；
    2. 有图/贴章/OLE/分页符/分节符 → 段里有内容或换页语义，删了就少一张图、少一次换页；
    3. `pPr` 里有下边框、或 run 里有下划线 → 原件靠它「画一条线给人签名」，
       看着空，其实删了签字栏就没了。
    """
    if getattr(element, "tag", None) != qn("w:p"):
        return False
    if any((node.text or "").strip() for node in element.iter(qn("w:t"))):
        return False
    for tag in ("w:drawing", "w:pict", "w:object"):
        if element.find(".//" + qn(tag)) is not None:
            return False
    for br in element.iter(qn("w:br")):
        if br.get(qn("w:type")) == "page":
            return False
    if element.find(".//" + qn("w:sectPr")) is not None:
        return False
    for ppr in element.iter(qn("w:pPr")):
        borders = ppr.find(qn("w:pBdr"))
        if borders is not None and borders.find(qn("w:bottom")) is not None:
            return False
    for rpr in element.iter(qn("w:rPr")):
        if rpr.find(qn("w:u")) is not None:
            return False
    return True


def _strip_tail_blanks(elements: list, keep: int = 1) -> list:
    """剪掉切片**尾部**的凑页空段：原件常用空行把内容顶到下一页，搬进成稿就是一整页白纸。

    实测「十二、其他资料」这一片的全部内容 = 标题 1 段 + 空段 18 段，18 段空行足够撑满
    一整页，于是标题后面先出一张白纸。这里从尾部往回删、只留 `keep` 段，章标题下面不会
    再空一整页。

    只动尾部：中部连续空段（如「四、投标保证金」里贴证照的 16 段留白）一个都不碰——
    那是原件的排版意图，不是凑页；要不要封顶需要产品确认（见诊断 §四 修复 3）。
    """
    items = list(elements or ())
    tail = 0
    while tail < len(items) and _is_removable_blank(items[-1 - tail]):
        tail += 1
    drop = tail - max(0, int(keep))
    if drop <= 0:
        return items
    cut = items[len(items) - tail: len(items) - tail + drop]
    for element in cut:
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    kept = items[:len(items) - tail] + items[len(items) - tail + drop:]
    return kept or items[-1:]      # 全空切片也至少留一段，别把整章删没


# 零宽标记：不影响"段末是什么"，判分页符时按不存在处理
_ZERO_WIDTH_TAGS = frozenset(qn(tag) for tag in (
    "w:bookmarkStart", "w:bookmarkEnd", "w:proofErr", "w:lastRenderedPageBreak",
    "w:commentRangeStart", "w:commentRangeEnd", "w:commentReference",
    "w:permStart", "w:permEnd",
))


def _tail_page_break(element: Any) -> Any:
    """段落**末尾**那个分页符节点（`w:br w:type="page"`），没有则返回 None。

    只看末尾：原件章内写在段中间的换页（一个标题下面分段）是有意义的排版，不能当成
    「章末换页」处理。段末的空 run（转换件常留 `<w:r><w:rPr/></w:r>`）跳过，不算内容。

    书签/校对标记这类**零宽节点**一律跳过再判：实测 ¶394（「十一、技术服务和质保期服务
    计划」）的 br 后面就跟着 `w:bookmarkEnd`，若不跳过，这处双换页会被漏掉。
    """
    if getattr(element, "tag", None) != qn("w:p"):
        return None

    def _zero_width(node: Any) -> bool:
        return getattr(node, "tag", None) in _ZERO_WIDTH_TAGS

    for child in reversed([node for node in element if getattr(node, "tag", None)]):
        if child.tag == qn("w:pPr") or _zero_width(child):
            continue
        if child.tag != qn("w:r"):
            return None            # 末尾不是 run（表格/内容控件…）：不做判定
        runs = [node for node in child if getattr(node, "tag", None) and not _zero_width(node)]
        if not runs:
            continue               # 空 run：跳过，继续看前一个
        last = runs[-1]
        if last.tag == qn("w:br") and last.get(qn("w:type")) == "page":
            return last
        return None
    return None


def _drop_node(node: Any) -> None:
    """把一个节点从树上摘掉；摘完 run 里只剩 `rPr` 就顺手把空 run 也删了。"""
    if node is None:
        return
    parent = node.getparent()
    if parent is None:
        return
    parent.remove(node)
    rest = [child for child in parent if getattr(child, "tag", None) != qn("w:rPr")]
    if getattr(parent, "tag", None) == qn("w:r") and not rest:
        grand = parent.getparent()
        if grand is not None:
            grand.remove(parent)


def _force_page_break(elements: list) -> None:
    """让这一章从新的一页开始。

    原件里章与章之间靠分页符隔开，切出来的这一节不一定带着那个分页符；之前几章连着
    排下来就会「没有分页符」。这里在首段挂 pageBreakBefore（不是插入分页段，不会多出白纸）。

    **但换页只能有一下**：切片把原件的「分节符（下一页）」搬成了上一段段末的
    `w:br type=page`，这里再给本章首段挂 pbb，两处换页之间那一页一个字都没有——
    实测 9 处（¶83→84、¶190→191、¶219→220、¶329→330、¶343→344、¶370→371、
    ¶391→392、¶393→394、¶394→395）。所以挂 pbb 之前先看前一块：

    * 前一块空段 + 段末 br → 那个 br 是纯换页填充，删掉它，换页交给本章的 pbb；
    * 前一块有字 + 段末 br → 上一章已经换过页了，本章不要再挂 pbb（直接 return）。

    注意**不要**因为本章首段自己带 br 就跳过 pbb：首段段末的 br 管的是「本段之后换页」，
    管不到本段的起点，跳过它反而会让这一章接着上一章排。
    """
    from docx.oxml import OxmlElement as _Ox

    first = None
    for element in elements or ():
        if getattr(element, "tag", None) == qn("w:p"):
            first = element
            break
        if getattr(element, "tag", None) == qn("w:tbl"):
            break
    if first is None:
        return
    prev = first.getprevious()
    brk = _tail_page_break(prev) if prev is not None else None
    if brk is not None:
        has_text = any((node.text or "").strip() for node in prev.iter(qn("w:t")))
        if has_text:
            return                 # 上一段有字且已在段末换页：本章不用再挂 pbb
        _drop_node(brk)            # 上一段是空段：删掉这个 br，换页交给下面的 pbb
    ppr = first.find(qn("w:pPr"))
    if ppr is None:
        ppr = _Ox("w:pPr")
        first.insert(0, ppr)
    if ppr.find(qn("w:pageBreakBefore")) is not None:
        return
    after = ("w:pStyle", "w:keepNext", "w:keepLines", "w:widowControl")
    index = 0
    for position, child in enumerate(list(ppr)):
        if child.tag in {qn(tag) for tag in after}:
            index = position + 1
    ppr.insert(index, _Ox("w:pageBreakBefore"))


def _section_text_width(sect_pr: Any) -> int:
    """一节的可排版宽度（twips）= 纸宽 − 左页边距 − 右页边距；读不到就返回 0。

    版心右界必须**按节**算：成稿里封面/目录（平台页边距）、原件章（原件的页边距）、
    PDF 章（页边距 0）各是一节，纸宽一样而版心宽度差几千 twip。
    """
    if sect_pr is None:
        return 0
    try:
        page = sect_pr.find(qn("w:pgSz"))
        margin = sect_pr.find(qn("w:pgMar"))
        width = int(page.get(qn("w:w")))
        left = int(margin.get(qn("w:left")))
        right = int(margin.get(qn("w:right")))
    except (AttributeError, TypeError, ValueError):
        return 0
    return width - left - right if width > 0 else 0


def _body_section_limits(body: Any) -> list[int]:
    """按**文档顺序**列出每一节的版心宽度（twips），与正文内容一一对应。

    Word 的规矩：段落 `pPr` 里的 `w:sectPr` 是**它前面那一段内容**的节属性，body 末尾
    那个 `w:sectPr` 管最后一节。所以第 i 个 sectPr 的宽度就是第 i 段内容的版心。
    只读 `doc.sections[-1]`（旧实现）等于把整篇都按最后一节算 —— 原件章插在中间、后面
    又切回平台节时，前面章节的制表位就按平台的版心（例如 1587 页边距的 8732）「夹」，
    夹完仍然大于原件章的版心（1800 页边距的 8306），看起来就是"根本没生效"。
    """
    limits: list[int] = []
    for child in body:
        if getattr(child, "tag", None) != qn("w:p"):
            continue
        ppr = child.find(qn("w:pPr"))
        if ppr is None:
            continue
        sect = ppr.find(qn("w:sectPr"))
        if sect is not None:
            limits.append(_section_text_width(sect))
    tail = body.find(qn("w:sectPr"))
    if tail is not None:
        limits.append(_section_text_width(tail))
    return limits


def _clamp_paragraph_tabs(paragraph: Any, limit: int) -> None:
    """把一个段落里越界的制表位拉回 `limit`，并补上画下划线用的右制表位。"""
    if limit <= 0:
        return
    for tabs in paragraph.iter(qn("w:tabs")):
        for tab in list(tabs):
            if tab.tag != qn("w:tab"):
                continue
            pos = tab.get(qn("w:pos"))
            try:
                value = int(pos)
            except (TypeError, ValueError):
                continue
            if value > limit:
                tab.set(qn("w:pos"), str(limit))
    # 有「下划线 run + tab」却没写制表位的段落：补一条右制表位，否则下划线画不出来。
    has_tab = False
    underlined_tab = False
    for run in paragraph.iter(qn("w:r")):
        if run.find(qn("w:tab")) is None:
            continue
        has_tab = True
        rpr = run.find(qn("w:rPr"))
        if rpr is not None and rpr.find(qn("w:u")) is not None:
            underlined_tab = True
    if not (has_tab and underlined_tab):
        return
    ppr = paragraph.find(qn("w:pPr"))
    if ppr is None:
        ppr = paragraph.makeelement(qn("w:pPr"), {})
        paragraph.insert(0, ppr)
    if ppr.find(qn("w:tabs")) is not None:
        return
    tabs = ppr.makeelement(qn("w:tabs"), {})
    tab = ppr.makeelement(qn("w:tab"), {qn("w:val"): "right", qn("w:pos"): str(limit)})
    tabs.append(tab)
    ppr.insert(0, tabs)


def _clamp_tab_stops(doc: Any) -> None:
    """把越界的右制表位拉回**本节**版心右边界，让下划线空白的长度等于原件。

    招投标模板里那些空白下划线不是一串下划线字符，而是「右制表位 + 下划线的 tab」：
    下划线画多长，等于制表位到版心右边的距离。`.docx` 原件里 `w:pos` 正好停在右边距；
    `.doc` 经 LibreOffice 转换后 `w:pos` 会被重写、常常越界（实测 8732）或按 709 的
    默认制表位重排，于是下划线短一截（用户看到的“下滑线与模板长度不匹配”）。

    规则只有一条：**越界才改**。
    · `.docx` 原件的 pos 本来就在版心内 → 一个字节都不动，仍逐字节保真；
    · `.doc` 转换后的越界 pos → 改成版心宽度，下划线正好画到右边距。
    两种格式共用同一条规则，所以可以同时支持 doc 和 docx。

    上限按**每个段落自己所属的那一节**取（`_body_section_limits`），不是整篇一个数。
    本函数可以重复调用：已经在版心内的 pos 不再改动，所以再跑一次只会把「后来因为
    多插了一个分节符而换了版心」的那些制表位修正过来。
    """
    element = getattr(doc, "element", None)
    body = getattr(element, "body", None)
    if body is None:
        return
    limits = _body_section_limits(body)
    if not limits:
        # 没有任何 sectPr（异常文档）：退回按最后一节算，至少不把整篇放弃掉
        sections = getattr(doc, "sections", None)
        try:
            section = sections[-1]
            limits = [int(section.page_width.twips) - int(section.left_margin.twips)
                      - int(section.right_margin.twips)]
        except Exception:  # noqa: BLE001
            return
        for paragraph in body.iter(qn("w:p")):
            _clamp_paragraph_tabs(paragraph, limits[0])
        return
    index = 0
    limit = limits[0] if limits else 0
    for child in list(body):
        tag = getattr(child, "tag", None)
        if tag == qn("w:sectPr"):
            continue          # body 级节属性只管最后一节，不是内容
        if limit > 0:
            for paragraph in ([child] if tag == qn("w:p") else child.iter(qn("w:p"))):
                _clamp_paragraph_tabs(paragraph, limit)
        if tag != qn("w:p"):
            continue
        ppr = child.find(qn("w:pPr"))
        if ppr is None or ppr.find(qn("w:sectPr")) is None:
            continue
        index += 1            # 这个段落封住了本节：后面的内容按下一节的版心算
        limit = limits[index] if index < len(limits) else 0


def _style_declared_size(styles: Any, style_id: str) -> int | None:
    """某个样式（顺着 `w:basedOn` 往上找）写死的字号（半磅）；没有就返回 None。"""
    seen: set[str] = set()
    current = str(style_id or "")
    while current and current not in seen and len(seen) < 12:
        seen.add(current)
        node = None
        for style in styles.findall(qn("w:style")):
            if style.get(qn("w:styleId")) == current:
                node = style      # 同名样式被重复导入时取最后一份（与 Word 一致）
        if node is None:
            return None
        rpr = node.find(qn("w:rPr"))
        size = rpr.find(qn("w:sz")) if rpr is not None else None
        if size is not None:
            try:
                return int(size.get(qn("w:val")))
            except (TypeError, ValueError):
                return None
        based = node.find(qn("w:basedOn"))
        current = str(based.get(qn("w:val")) or "") if based is not None else ""
    return None


def _style_default_size(styles: Any) -> int | None:
    """文档级默认字号（`docDefaults/w:rPrDefault/w:rPr/w:sz`，半磅）。"""
    try:
        defaults = styles.find(qn("w:docDefaults"))
        rpr = defaults.find(qn("w:rPrDefault")).find(qn("w:rPr"))
        size = rpr.find(qn("w:sz"))
        return int(size.get(qn("w:val")))
    except (AttributeError, TypeError, ValueError):
        return None


def _default_paragraph_style_size(styles: Any) -> int | None:
    """没写 `w:pStyle` 的段落用的那个默认段落样式的字号（半磅）。

    `.doc` 转出来的表格单元格常常一个 `w:pStyle` 都没有，那时字号来自
    `docDefaults` 之上再叠一层的默认段落样式（原件的 Normal），不能只看 docDefaults。
    """
    for style in styles.findall(qn("w:style")):
        if str(style.get(qn("w:type")) or "") != "paragraph":
            continue
        if str(style.get(qn("w:default")) or "") not in ("1", "true", "on"):
            continue
        size = _style_declared_size(styles, str(style.get(qn("w:styleId")) or ""))
        if size:
            return size
    return None


# w:sz / w:szCs 在 w:rPr 里必须排在下面这些内联格式**前面**，否则 Word 判文档损坏
_RPR_AFTER_SIZE = ("w:szCs", "w:highlight", "w:u", "w:effect", "w:bdr", "w:shd", "w:fitText",
                   "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang", "w:eastAsianLayout",
                   "w:specVanish", "w:oMath")


def _run_text(run: Any) -> str:
    return "".join(node.text or "" for node in run.iter(qn("w:t")))


def _run_in_table(run: Any) -> bool:
    parent = run.getparent()
    while parent is not None:
        if getattr(parent, "tag", None) == qn("w:tbl"):
            return True
        parent = parent.getparent()
    return False


def _run_size(rpr: Any) -> int | None:
    node = rpr.find(qn("w:sz")) if rpr is not None else None
    if node is None:
        return None
    try:
        return int(node.get(qn("w:val")))
    except (TypeError, ValueError):
        return None


def _set_run_size(rpr: Any, size: int) -> None:
    node = rpr.makeelement(qn("w:sz"), {qn("w:val"): str(size)})
    rpr.insert_element_before(node, *_RPR_AFTER_SIZE)
    if rpr.find(qn("w:szCs")) is None:
        cs = rpr.makeelement(qn("w:szCs"), {qn("w:val"): str(size)})
        rpr.insert_element_before(cs, *_RPR_AFTER_SIZE[1:])


def _drop_run_size(rpr: Any) -> bool:
    dropped = False
    for name in ("w:sz", "w:szCs"):
        node = rpr.find(qn(name))
        if node is not None:
            rpr.remove(node)
            dropped = True
    return dropped


# 带编号的行（7.2 / 一、 / （一） / 第X章）：这些是标题，字号是原作者定的，一个字都不许动
_NUMBERED_LINE = re.compile(
    r"^[\s\u3000]*(?:第[一二三四五六七八九十百千\d]+[章节部分条]"
    r"|[（(][一二三四五六七八九十百\d]+[）)]"
    r"|[一二三四五六七八九十百\d]{1,3}\s*[、.．)）]"
    r"|\d+(?:\.\d+)+\s*)")


def _is_numbered_line(text: str) -> bool:
    return bool(_NUMBERED_LINE.match(str(text or "")))


def _adopt_original_text_size(elements: list, blob: bytes) -> int:
    """让原件章里的字号跟随**原件段落/表格样式**，不被 `.doc` 转换件带偏。

    实测病因既不是 `w:tblStyle` 没搬（`_style_refs`/`_import_styles`/
    `_remap_style_refs` 三处都覆盖它，而且 `投标书格式.docx` 的表格压根不带 `w:tblStyle`），
    也不是单元格 `rPr` 继承了平台默认样式。真实的因果链有两层：

    1. **段落标记的字号是「空行高度」，不是正文字号**。原件里「7.x …分项报价表」标题下面
       那一行「单位：人民币  元」，段落标记 `w:pPr/w:rPr` 被格式化成 16pt 加粗黑体
       （`sz=32`+`b`，用来撑行高），而**正文 run 一个字号都没写**，靠段落样式
       （Normal，10.5/11pt 宋体）落字。`.doc` 经 LibreOffice 转 docx 时，转换器把段落标记
       的格式抄到了正文 run 上，于是这一行文字真的变成 16pt 加粗黑体——比原件大 4~5pt。
       实测该章 5 段纯空白的「单位」行全是 `sz=32/30`+`eastAsia=黑体`，而段落标记与它们
       一模一样；同章的 `7.2 必备的…` 标题 run 本来就写了 `sz=30`（原件如此），不能动。
       所以这里只对**纯空白 run**（没有任何可见字、只用来对齐的空格）把抄来的字号摘掉，
       让它回到原件的段落样式；段落标记原样保留，行高一个 twip 都不变。
    2. **表格单元格的字号常常根本没写**（转换件只写 `w:szCs`），样式链上再取不到就落到
       「目标文档的默认值」上：`docDefaults`（12pt）或平台 `Normal`（导出时按"正文字号"
       设的 12/16pt）。原件表格是 10.5pt 宋体，成稿就大一号。这里把表格里没写字号的
       **有字** run 补成原件表格样式的字号，落到哪里都不再受平台默认值影响。

    字号一律从**切片自己的样式表**里读（成稿里既有平台 Normal 又有搬进来的 ZBT 样式，
    默认样式分辨不出谁是谁）。原件显式写了字号的 run（标题行、7.x 标题）一律不动，
    所以 docx 原件仍然逐字节保真。
    """
    import io as _io

    from docx import Document as _Document

    items = [el for el in elements or () if isinstance(getattr(el, "tag", None), str)]
    if not items:
        return 0
    try:
        slice_doc = _Document(_io.BytesIO(bytes(blob)))
    except Exception:  # noqa: BLE001 —— 打不开切片就不补，保持原样
        return 0
    styles = slice_doc.styles.element
    if styles is None:
        return 0
    source = [el for el in slice_doc.element.body
              if isinstance(el.tag, str) and el.tag != qn("w:sectPr")]
    fallback: list[int | None] = []

    def _style_size(paragraph: Any) -> int | None:
        ppr = paragraph.find(qn("w:pPr"))
        style = ppr.find(qn("w:pStyle")) if ppr is not None else None
        style_id = str(style.get(qn("w:val")) or "") if style is not None else ""
        size = _style_declared_size(styles, style_id) if style_id else None
        if size:
            return size
        if not fallback:
            fallback.append(_default_paragraph_style_size(styles)
                            or _style_default_size(styles))
        return fallback[0]

    changed = 0
    for position, (target, origin) in enumerate(zip(items, source)):
        if getattr(target, "tag", None) != getattr(origin, "tag", None):
            continue
        for index, (paragraph, original) in enumerate(
                zip(target.iter(qn("w:p")), origin.iter(qn("w:p")))):
            size = _style_size(original)
            if not size:
                continue
            text = "".join(node.text or "" for node in paragraph.iter(qn("w:t")))
            ppr = original.find(qn("w:pPr"))
            mark = ppr.find(qn("w:rPr")) if ppr is not None else None
            mark_size = _run_size(mark)
            # 一章的第一段是章标题、带编号的行是小节标题：字号都由原件定，不参与下面的还原
            keep = (position == 0 and index == 0) or _is_numbered_line(text)
            for run in paragraph.iter(qn("w:r")):
                rpr = run.find(qn("w:rPr"))
                explicit = _run_size(rpr)
                visible = bool(_run_text(run).strip())
                if explicit is not None:
                    # 「和段落标记同号、又比原件样式大」= 转换件把段落标记的格式抄到了
                    # run 上（原件里这一行正文本就没写字号，靠样式落字）。摘掉它，让这一
                    # 行回到原件样式的字号；行高仍由段落标记决定，版面不变。
                    if explicit > size and explicit == mark_size and (not visible or not keep):
                        changed += _drop_run_size(rpr)
                    continue
                if not visible or not _run_in_table(run):
                    continue          # 只补「表格里没写字号的有字 run」
                if rpr is None:
                    rpr = run.makeelement(qn("w:rPr"), {})
                    run.insert(0, rpr)
                _set_run_size(rpr, size)
                changed += 1
    return changed


def _adopt_document_defaults(doc: Any, blob: bytes) -> None:
    """把原件的「文档级默认值」搬过来（只搬一次）。

    切片的段落级格式是完整的，但**文档级**的三样东西还在平台上，于是：
    * `docDefaults` 的 `w:lang w:eastAsia` 是 en-US —— Word 认为中文标点该用西文字体
      排，Times New Roman 里没有「（）；……」这些全角字形，屏幕上就是一个个方框；
    * `docDefaults` 的行距 after=200/line=276，原件是 after=160/line=278 —— 每页行数
      就差出来了，页数、页码、分页位置全跟着错；
    * `settings.xml` 的 defaultTabStop 是 720，原件是 420 —— 投标人/地址那一列靠制表位
      右对齐，位置自然对不上。
    这三样都是文档级、跟章节无关，所以只搬第一份原件即可。
    """
    import copy as _copy
    import io as _io

    from docx import Document as _Document

    if getattr(doc, "_zbt_defaults_done", False):
        return
    source = _Document(_io.BytesIO(blob))
    try:
        src_styles = source.styles.element
        dst_styles = doc.styles.element
        src_defaults = src_styles.find(qn("w:docDefaults"))
        dst_defaults = dst_styles.find(qn("w:docDefaults"))
        if src_defaults is not None:
            if dst_defaults is not None:
                dst_styles.replace(dst_defaults, _copy.deepcopy(src_defaults))
            else:
                dst_styles.insert(0, _copy.deepcopy(src_defaults))
    except Exception:  # noqa: BLE001
        pass
    try:
        src_settings = source.settings.element
        dst_settings = doc.settings.element
        src_tab = src_settings.find(qn("w:defaultTabStop"))
        if src_tab is not None:
            dst_tab = dst_settings.find(qn("w:defaultTabStop"))
            if dst_tab is not None:
                dst_settings.replace(dst_tab, _copy.deepcopy(src_tab))
            else:
                dst_settings.append(_copy.deepcopy(src_tab))
    except Exception:  # noqa: BLE001
        pass
    doc._zbt_defaults_done = True  # noqa: SLF001


def _copy_section_grid(doc: Any, blob: bytes) -> None:
    """把原件的文档网格搬到本节。

    原件是 `docGrid type=lines linePitch=312`（每页固定行数），成稿拿到的是平台默认
    `linePitch=360`、没有 type：于是同一页少排三四行，分页位置就和原件对不上了。
    页眉/页脚距离同理，一并跟过来。
    """
    import copy as _copy
    import io as _io

    from docx import Document as _Document

    source = _Document(_io.BytesIO(blob))
    src = source.sections[-1]._sectPr
    dst = doc.sections[-1]._sectPr
    for tag in ("w:docGrid",):
        node = src.find(qn(tag))
        if node is None:
            continue
        old = dst.find(qn(tag))
        if old is not None:
            dst.remove(old)
        dst.append(_copy.deepcopy(node))
    src_mar = src.find(qn("w:pgMar"))
    dst_mar = dst.find(qn("w:pgMar"))
    if src_mar is None or dst_mar is None:
        return
    for attr in ("header", "footer"):
        value = src_mar.get(qn("w:" + attr))
        if value:
            dst_mar.set(qn("w:" + attr), value)


def _append_template_chapter(doc: Any, blob: bytes, *, toc: Any = None, level: int = 1,
                             page_setup: dict[str, Any] | None = None,
                             restore: dict[str, Any] | None = None,
                             new_page: bool = False) -> bool:
    """把招标文件对应小节的**原件版式**整段写进成稿，返回是否写过。

    版式来自招标原件（字体/字号/表格合并/边框/分页符都在 XML 里），这里只做两件
    不改变外观的事：给首段标大纲级别、给首段挂目录书签。任何异常都返回 False，
    调用方随即回落到历史文本渲染——导出是交付动作，宁可版式退回旧样式，也不能出空章。
    """
    try:
        # 页面设置由调用方按"章节类型"统一切换（见 _write_body_with_toc 里的 _ensure_section），
        # 这里只负责把切片内容原样落进来。
        from zhibiao.core.format_template import append_to_document

        elements = append_to_document(doc, blob)
        # `append_to_document` 返回的就是**树上那几个节点**（它对切片逐个 deepcopy 后
        # addprevious 进去，复制品才是树上的）。别按 body 尾部切片取：落点在末尾的
        # `w:sectPr` **之前**，切出来的片段永远是空的，`_zbt_tpl_p` 一个段落都登记不上。
        _inserted = list(elements or ())
    except Exception:  # noqa: BLE001
        return False
    from docx.oxml.ns import qn as _qn

    def _drop() -> None:
        # 引擎已经把这些元素追加进文档了；判定要回落就必须撤干净，
        # 否则成稿里会同时存在「模板切片」和「历史文本」两份内容。
        for element in elements or ():
            parent = getattr(element, "getparent", lambda: None)()
            if parent is not None:
                parent.remove(element)

    if not elements:
        return False
    # 招标文件里有「只有标题、下面留白」的小节（如「十、技术支持资料」），那不是切分错误，
    # 原件本来就是这样。只要切片里有字就照原样落；真的一字没有才回落，避免出空章。
    has_text = False
    for element in elements or ():
        if getattr(element, "tag", None) == _qn("w:tbl"):
            has_text = True
            break
        if any((t.text or "").strip() for t in element.findall(".//" + _qn("w:t"))):
            has_text = True
            break
    if not has_text:
        _drop()
        return False
    # 原件自带的那张小节目录不进标书（会和平台目录叠成两张）
    elements = _strip_leading_toc(elements)
    # 原件的尾部「凑页空段」也不进标书：18 段空行就是一整页白纸。
    # 必须在 `_force_page_break` **之前**做：判断「上一块是不是空段 + 段末 br」看的正是
    # 本章第一块的前一个兄弟，尾部空段已经删干净，判定才落在真正的内容上。
    elements = _strip_tail_blanks(elements)
    if new_page:
        _force_page_break(elements)
    try:
        # 企业名称/地址/开户行/账号/法定代表人/签字盖章/日期/报价这些客户自己的信息，
        # 平台一律不代填，只把原件里对应的占位文字标红，客户一眼看到该填哪儿。
        from zhibiao.core.format_template import mark_customer_fields

        mark_customer_fields(elements)
    except Exception:  # noqa: BLE001 —— 标红失败不影响把这一章按原件版式落下来
        pass
    try:
        first = elements[0]
        if getattr(first, "tag", None) == _qn("w:p"):
            _set_outline_level(first, level)
            from docx.text.paragraph import Paragraph
            para = Paragraph(first, doc._body)
            if level == 1 and str(para.text or "").strip():
                from docx.enum.text import WD_ALIGN_PARAGRAPH
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if toc is not None:
                text = str(para.text or "").strip()
                if text:
                    toc.add(para, text, level)
    except Exception:  # noqa: BLE001 —— 目录登记失败不影响把这一章写出来
        pass
    try:
        _adopt_document_defaults(doc, blob)
    except Exception:  # noqa: BLE001 —— 默认值搬不过去也要把这一章落下来
        pass
    try:
        _copy_section_grid(doc, blob)
    except Exception:  # noqa: BLE001 —— 网格搬不过去也不能影响这一章落稿
        pass
    try:
        # 表格字号跟原件走：转换件只写 szCs 不写 sz，物化时补不上，落到成稿默认值
        # 就"大一号"；空白段被抄上段落标记的 16pt 黑体也在这里还原。
        # 用 `elements`（append_to_document 返回的就是树上那几个节点）而不是 `_inserted`：
        # `_inserted` 是按 body 尾部切片取的，而落点在末尾的 sectPr **之前**，切不到内容。
        _adopt_original_text_size(elements, blob)
    except Exception:  # noqa: BLE001 —— 字号补不上也不能影响这一章落稿
        pass
    try:
        # 制表位按本节版心夹一次。后面还可能插入新的分节符（下一章换回平台/PDF 版心），
        # `_write_body_with_toc` 收尾时会再整体夹一遍，那一遍才是最终结果。
        _clamp_tab_stops(doc)
    except Exception:  # noqa: BLE001 —— 制表位重算失败就保留原样
        pass
    try:
        # 原件段落登记在文档上：导出的「统一行距/字号」必须跳过它们，
        # 否则 1.5 倍行距会套到原件上，一页少排十几行，页码跟原件对不上。
        marked = getattr(doc, "_zbt_tpl_p", None)
        if marked is None:
            marked = set()
            doc._zbt_tpl_p = marked  # noqa: SLF001 - 挂在本次导出的文档对象上
        # 光记 `id()` 不够：lxml 的元素代理对象没人引用就会被回收，之后 `doc.paragraphs`
        # 再拿到的是**另一个**代理，id 全变、登记等于没做。把节点本身也挂在 doc 上钉住，
        # id 才有意义（只在本次导出期间需要，doc 释放时一起释放）。
        pinned = getattr(doc, "_zbt_tpl_nodes", None)
        if pinned is None:
            pinned = []
            doc._zbt_tpl_nodes = pinned  # noqa: SLF001 - 同上
        for element in _inserted:
            # 目录/尾部空段已经被 `_strip_*` 从树上摘掉了：别再登记（节点被回收后 id 会复用）
            if element.getparent() is not None:
                marked.add(id(element))
                pinned.append(element)
    except Exception:  # noqa: BLE001
        pass
    return True


def _write_pdf_image_page(doc: Any, image: dict[str, Any], *, break_before: bool) -> Any:
    """把一页 PDF 渲染图铺成整页（页边距 0 的节里，图铺满版心）。"""
    import io as _io

    from docx.shared import Pt

    section = doc.sections[-1]
    avail = int(section.page_width) - int(section.left_margin) - int(section.right_margin)
    page_w, page_h = int(section.page_width), int(section.page_height)
    para = doc.add_paragraph()
    para.paragraph_format.page_break_before = bool(break_before)
    para.paragraph_format.space_before = Pt(0)
    para.paragraph_format.space_after = Pt(0)
    # 行距必须是「单倍」而不是固定 1 磅：`lineRule=exact` 配一张整页高的图，
    # Word 会按 1 磅行高裁掉图片，只剩一条缝。
    para.paragraph_format.line_spacing = 1.0
    width = avail
    ratio = 0.0
    try:
        ratio = float(image["height_pt"]) / float(image["width_pt"])
        if int(avail * ratio) > page_h:
            width = int(page_h / ratio)
    except Exception:  # noqa: BLE001
        pass
    # 图必须比版心**矮一点点**：整页高的图配"单倍行距"会多出一丝行高，把下一段挤到
    # 新的一页；那一页再被 `page_break_before` 顶一下，标题就整体下移一行（实测 +23.4pt），
    # 前面还会多出一张只有空行的白纸。留 2pt 余量即可，肉眼无差。
    try:
        # 注意单位：page_h 是 EMU，1pt = 12700 EMU，留 4pt 余量
        if ratio and int(width * ratio) >= page_h - 4 * 12700:
            width = max(1, int((page_h - 4 * 12700) / ratio))
    except Exception:  # noqa: BLE001
        pass
    run = para.add_run()
    run.add_picture(_io.BytesIO(image["png"]), width=max(1, width))
    return para


def _append_pdf_chapter(doc: Any, images: list[dict[str, Any]], title: str, *,
                        toc: Any = None, level: int = 1,
                        restore: dict[str, Any] | None = None) -> Any:
    """PDF 招标文件的格式小节：把该小节的**原页**逐页写进成稿，返回第一段落。

    三件必须做对的事：

    1. **不再另写一个正文标题**。原页里本来就有「一、投标函」这一行；再写一遍，成稿上
       就会出现两行一样的标题。所以这里只放页图，标题交给招标文件自己的页面。
    2. **目录仍然要有这一条**。把书签挂到第一张页图所在的段落上，目录条目指向的页码
       就是该小节真实所在页 —— 目录里点一下跳过去的，正是原件那一页。
    3. **页与页之间用 `page_break_before`，不插空分页段落**。插空段落迟早会在章与章
       之间多出一张白纸；`page_break_before` 不会。

    任何异常返回 None，调用方随即回落文本渲染。
    """
    try:
        # 页面的纸张与页边距由调用方（_ensure_section）统一切换，这里只管铺图
        paras: list[Any] = []
        for image in images or []:
            # 本节已经用分节符换页了，第一张再加"段前分页"就会多一张白纸
            paras.append(_write_pdf_image_page(doc, image, break_before=bool(paras)))
        if paras and toc is not None:
            # 目录条目挂在第一张页图上：页码=该小节在成稿里的真实起页
            toc.add(paras[0], title, level)
        return paras
    except Exception:  # noqa: BLE001 —— 转图失败就回落文本渲染，不能因此出不了件
        return None


def _append_pdf_pages(doc: Any, pdf_source: bytes, page_nos: list[int], title: str, *,
                      toc: Any = None, level: int = 1, new_page: bool = False) -> Any:
    """将 PDF 页重建成可编辑文本与表格；扫描页仅使用 OCR 文本。"""
    import io as _io
    import pdfplumber
    from zhibiao.core import pdf_rebuild

    body = doc.element.body
    original = set(body)
    paras: list[Any] = []
    current = None
    previous_example = False
    example_hint = ""
    previous_page = None
    try:
        stream = _io.BytesIO(pdf_source) if isinstance(pdf_source, (bytes, bytearray)) else str(pdf_source)
        with pdfplumber.open(stream) as pdf:
            for current in page_nos:
                number = int(current)
                if previous_page is not None and number != previous_page + 1:
                    previous_example = False
                    example_hint = ""
                previous_page = number
                if not 0 <= number < len(pdf.pages):
                    raise ValueError("页码超出原件范围")
                page = pdf.pages[number]
                analysis = pdf_rebuild.analyze_page(page)
                br = bool(paras) or bool(new_page)
                # A page with embedded raster content needs OCR; simply rebuilding
                # its text layer would silently discard the scanned image content.
                text_size = sum(len(str(w.get("text") or "")) for line in analysis.get("lines", []) for w in line.get("words", []))
                source_text = str(page.extract_text() or "")
                example = bool(page.images and (re.search(r"截图示例|示例截图|示例图|模板[^。\n]{0,25}(?:类型|示例)|(?:截图|图示)[^。\n]{0,25}(?:参考|示例|如图)|(?:参考|示例)[^。\n]{0,60}(?:截图|如图)", source_text)
                                                or (previous_example and text_size <= 30)))
                previous_example = example
                if example:
                    if re.search(r"开户许可证|基本存款账户", source_text):
                        example_hint = "开户许可证或基本存款账户信息复印件"
                    elif re.search(r"信用|信誉|失信", source_text):
                        example_hint = "按原文指定渠道提供真实信誉查询网页截图"
                    elif re.search(r"管理体系|认证", source_text):
                        example_hint = "管理体系认证状态查询截图"
                    elif not example_hint:
                        example_hint = "按本节招标要求提供真实证明材料"
                    if int(analysis.get("tables") or 0) != len(analysis.get("table_blocks") or []):
                        raise ValueError("表格结构未能完整识别，请使用可编辑原件或先人工核对")
                    written = pdf_rebuild.write_page(doc, analysis, break_before=br,
                                                      trim_top_pt=20.3 if br else 18.6)
                    if not written:
                        paragraph = doc.add_paragraph()
                        paragraph.paragraph_format.page_break_before = br
                        written = [paragraph]
                    _md_add_image_slot(doc, "{{图片占位:" + example_hint + "}}")
                elif (not page.images and not text_size and not analysis.get("table_blocks")
                        and not page.lines and not page.rects and not page.curves):
                    blank = doc.add_paragraph()
                    blank.paragraph_format.page_break_before = br
                    written = [blank]
                elif page.images or (not text_size and not analysis.get("table_blocks")):
                    image = page.to_image(resolution=150).original
                    text, _how = _ocr_bounded(image, "chi_sim+eng")
                    if not str(text or "").strip():
                        raise ValueError("扫描内容未识别出文字，请使用可识别的原件或先完成 OCR")
                    note = doc.add_paragraph("【本页为图片内容的文字识别结果，文字和格式须与原件核对；图形、印章及签名需人工补齐】")
                    note.paragraph_format.page_break_before = br
                    written = [note]
                    for line in str(text).splitlines():
                        if line.strip():
                            written.append(doc.add_paragraph(line.strip()))
                else:
                    if int(analysis.get("tables") or 0) != len(analysis.get("table_blocks") or []):
                        raise ValueError("表格结构未能完整识别，请使用可编辑原件或先人工核对")
                    written = pdf_rebuild.write_page(doc, analysis, break_before=br,
                                                      trim_top_pt=20.3 if br else 18.6)
                    if not written:
                        raise ValueError("页面没有可转换的文字或表格")
                paras.extend(written)
        if paras and toc is not None:
            toc.add(paras[0], title, level)
        return paras
    except Exception as exc:
        for child in list(body):
            if child not in original and child.tag != qn("w:sectPr"):
                body.remove(child)
        page_label = str(int(current) + 1) if current is not None else "未知"
        raise HTTPException(status_code=422, detail="PDF 第%s页无法转换为可编辑内容：%s" % (page_label, str(exc))) from exc


def _has_own_numbering(title: str) -> bool:
    """标题是否自带编号（一、/（一）/1./第X章）——自带就不要再加平台章号。"""
    import re as _re

    value = str(title or "").strip()
    if not value:
        return False
    if _re.match(r"^\s*第[一二三四五六七八九十百\d]+[章节部分]", value):
        return True
    return bool(_re.match(r"^\s*(?:[（(]?[一二三四五六七八九十百\d]{1,3}[）)、]|"
                          r"[一二三四五六七八九十百]{1,3}\s*[、.．])", value))


_CN_DIGITS = "零一二三四五六七八九"


def _cn_ordinal(value: Any) -> str:
    """1 → 一、10 → 十、14 → 十四；认不出就原样返回。"""
    try:
        number = int(str(value or "").split(".")[0])
    except (TypeError, ValueError):
        return str(value or "")
    if 1 <= number <= 10:
        return "十" if number == 10 else _CN_DIGITS[number]
    if number < 20:
        return "十" + _CN_DIGITS[number % 10]
    if number < 100:
        return _CN_DIGITS[number // 10] + "十" + (
            _CN_DIGITS[number % 10] if number % 10 else ""
        )
    return str(number)


def _write_body_with_toc(doc: Any, rows: list[dict[str, Any]], *, toc_on: bool = True,
                         toc_level: int = 3, chinese_top: bool = False,
                         chapter_numbers: dict[str, str] | None = None,
                         containers: set[str] | None = None,
                         images: dict[str, bytes] | None = None,
                         templates: dict[str, bytes] | None = None,
                         pdf_source: bytes | None = None,
                         pdf_titles: dict[str, str] | None = None,
                         pdf_skip_pages: set[int] | None = None,
                         source_facts: dict[str, str] | None = None,
                         tender_text: str = "") -> int:
    """写「目录页 + 正文」，返回写入的目录条目数。

    目录条目与正文标题出自同一份清单（`_TocCollector`）：正文每写一个 Heading 1~3
    就登记一条并挂书签，目录只按这份清单出条目，不存在两处各算一遍。空标题
    （只剩编号的章，如「4.7」）与 Heading 4 不登记，所以也不会出现在目录里。
    """
    _chapter_numbers: dict[str, str] = dict(chapter_numbers or {})
    if not _chapter_numbers:
        from zhibiao.core.chapter_rules import section_numbers
        _chapter_numbers = section_numbers([dict(r) for r in rows or []])
    _containers = set(containers or ())
    toc = _TocCollector() if toc_on else None
    anchor = None
    if toc is not None:
        _add_toc_title(doc)  # 目录标题自己不进目录
        anchor = doc.add_paragraph()  # 占位：正文写完后把域+缓存条目插到它前面
        doc.add_page_break()
    rows = list(rows or [])
    _tpl = dict(templates or {})
    if _tpl:
        # 招标原件的目录只列「一、」「（一）」两级。平台默认排到三级，补章里模型写的小标题
        # 也会被收进来（13.6.11 这种），目录一下多出十几行、比原件多一整页。
        toc_level = min(int(toc_level or 3), 2)
    _used_pages: set[int] = set(pdf_skip_pages or ())   # 封面和原件目录已经单独处理
    _last_pdf_page = None            # PDF：上一章第一张页图所在段落（共用页的目录条目挂这里）
    # 平台自己的页面设置：模板章节用原件的纸张，写完要切回来，否则后面的章节跟着变形
    _platform_geom = {name: int(getattr(doc.sections[-1], name))
                      for name in _GEOM_FIELDS
                      if getattr(doc.sections[-1], name, None)}
    _word_setup: dict[str, Any] = {}
    _pdf_paper_done = False
    _pdf_geom: dict[str, Any] = {}
    _section_key = [_geometry_key(_platform_geom)]
    _word_pages = [0]   # 已经落过几章原件版式：从第二章起每章另起一页
    _pdf_pages_started = [False]   # 格式章是否已经落过内容（之后每节都要自己分页）
    _platform_started = [False]    # 平台版式的一级章：第二章起每章另起一页

    def _setup_for(mode: str) -> dict[str, Any]:
        if mode == "word" and _word_setup:
            return _word_setup
        if mode == "pdf" and _pdf_geom:
            return _pdf_geom
        return _platform_geom

    def _ensure_section(mode: str = "pdf") -> None:
        """按章节类型切换页面设置（同一页面几何连续章节只切一次，避免一节一章的碎片）。

        平台章：导出时选的页边距；Word 模板章：招标原件的纸张+页边距；
        PDF 模板章：A4 且页边距为 0（原页自带白边，铺满整页才和原件一致）。

        「同一节」只按**页面几何**（纸张 + 页边距 + 页眉页脚距离）判定，`mode` 不参与：
        mode 只用来挑 `_setup_for` 的那份设置。之前的键是 `mode/几何`，于是
        「十二、其他资料」（模板章，word 几何）后面紧跟的 12.x（平台章，几何一模一样）
        也会被判成"换了一节"，硬插一个只带 `w:sectPr` 的空段 = 一次强制分页，
        和上一章的标题之间就夹出一张空白页。同一页面几何共用一节，
        Word 里既不会多一次分页，页眉页脚也是同一套（页眉本来就是平台统一写的）。

        几何确实不同的章仍然各自成节：招标文件自己各节的页边距就不一样
        （第 1 节 1797/1843、第 2-9 节 1800/1800），右侧空白下划线（右制表位画到
        版心右界）必须按本节版心算，差几十 twip 就能看出来。
        """
        setup = _setup_for(mode)
        key = _geometry_key(setup)
        if _section_key[0] == key:
            return
        _push_section(doc, setup)
        _section_key[0] = key
    for r in rows:
        _num = _chapter_numbers.get(str(r.get("id") or r.get("seq") or ""), "")
        _title = _row_title(r)
        # Author-written response sections and manual edits must not be replaced
        # by an empty source form (Word or a rasterised PDF page).
        _manual = is_manual_content(r.get("source"))
        _body_content = str(r.get("content") or "")
        _authored = requires_authored_content(_title,_body_content,str(r.get('source') or ''))
        if _authored and not _manual and (is_instruction_only(_body_content, _title)
                or (str(r.get("source") or "") == "format"
                    and re.search(r"信誉要求|信用查询|管理体系认证", _title))):
            _body_content = source_backed_section(_title, tender_text)
        if _authored or _manual:
            _ensure_section("platform")
            _num = _chapter_numbers.get(str(r.get("id") or r.get("seq") or ""), "")
            if _has_own_numbering(_title):
                _heading = _title
            elif chinese_top and int(r["level"]) == 1:
                _heading = f"{_cn_ordinal(_num)}、{_title}"
            else:
                _heading = ("%s %s" % (_num, _title)).strip()
            _level = min(max(int(r["level"]), 1), 4)
            _paragraph = _add_export_heading(doc, _heading, _level, toc=toc,
                                             record=bool(_title.strip()))
            # 正式投标文件的一级章从新的一页开始；第一本草稿紧跟目录页，不再多分一页。
            if _level == 1:
                if _platform_started[0]:
                    _paragraph.paragraph_format.page_break_before = True
                _platform_started[0] = True
            _md_render_chapter(doc, _body_content, r["level"], r["title"], images,
                               chapter_number=str(_num), container=str(_num) in _containers, toc=toc)
            continue
        # 招标文件给了这一节的格式：整节按原件的 XML 落稿（字体/表格/分页符都在里面），
        # 不再走「文本 → 重排」。编号由模板自带，所以这里也不再加平台章号前缀。
        _blob = _tpl.get(str(r.get("id") or ""))
        _pdf_title = (pdf_titles or {}).get(str(r.get("id") or ""))
        # 有原件版式的章节：标题已经自带「一、」「（一）」「第六章」，再加平台章号就成了
        # 「8 （二）分项报价表」这种双编号；所以这种情况下标题原样用。
        if _has_own_numbering(_title):
            # 标题自己带「一、」「（一）」「第六章」：不管有没有原件版式，都不再加平台章号，
            # 否则目录里就是「1 一、投标函」这种双编号。
            _text = _title
        elif chinese_top and int(r["level"]) == 1:
            _text = f"{_cn_ordinal(_num)}、{_title}" if _num else _title
        else:
            _text = ("%s %s" % (_num, _title)).strip() if _num else _title
        _level = min(max(int(r["level"]), 1), 4)
        if _blob:
            try:
                from zhibiao.core.format_template import slice_page_setup

                _chapter_setup = dict(slice_page_setup(_blob) or {})
            except Exception:  # noqa: BLE001
                _chapter_setup = {}
            if not _word_setup:
                _word_setup = _chapter_setup
                _platform_geom = _adopt_paper(doc, _word_setup, _platform_geom)
            elif _chapter_setup and \
                    _geometry_key(_chapter_setup) != _geometry_key(_word_setup):
                # 这一节原件的版心和上一节不同：换成本节的版心，
                # 后面那句 `_ensure_section` 会因此新起一节（不同几何）。
                _word_setup = _chapter_setup
            _ensure_section("word")
            if _append_template_chapter(doc, _blob, toc=toc, level=_level,
                                          new_page=_word_pages[0] > 0):
                _word_pages[0] += 1
                continue
        # PDF 招标文件：把该小节所在的页逐页转图落进成稿（只看不改，页面外观与原件一致）
        if _pdf_title and pdf_source:
            if not _pdf_paper_done:
                # PDF 也是 A4：先把整篇换成 A4，再按页铺图（页面自身尺寸 595x842pt = A4）
                _pdf_paper_done = True
                _platform_geom = _adopt_paper(
                    doc, {"page_width": 7560310, "page_height": 10692130}, _platform_geom)
            _page_nos: list[int] = []
            _pages: list[dict[str, Any]] = []
            try:
                from zhibiao.core import pdf_format_template

                if isinstance(_pdf_title, (tuple, list)) and len(_pdf_title) >= 3:
                    # 页码在上传时已入库：直接按页号渲染，不再重扫文字层。
                    # 相邻小节常共用一页（父小节与它下面的第一个表在同一页），
                    # 所以按页码去重：这一页已经落过就不再落第二遍。
                    _first, _last = int(_pdf_title[1]), int(_pdf_title[2])
                    _skip_pages = pdf_skip_pages or set()
                    _page_nos = [p for p in range(_first, _last + 1)
                                 if p not in _used_pages and p not in _skip_pages]
                    if not _page_nos and _last >= _first:
                        # 这一小节就是已单独排版的封面页/原件目录，不再重复铺图
                        continue
                    _heading = str(_pdf_title[0])
                else:
                    _wanted = pdf_format_template.normalize_title(str(_pdf_title))
                    _sections = pdf_format_template.extract_sections(pdf_source)
                    _found = next((sec for sec in _sections if sec.get("match_key") == _wanted or
                                   pdf_format_template.normalize_title(str(sec.get("title") or "")) == _wanted), None)
                    _skip_pages = pdf_skip_pages or set()
                    if _found:
                        _page_nos = [p for p in range(int(_found["start_page"]) - 1, int(_found["end_page"]))
                                     if p not in _used_pages and p not in _skip_pages]
                    _heading = _text
            except Exception:  # noqa: BLE001
                _pages = []
            if _page_nos or _pages:
                if not _pdf_geom:
                    # 几何取第一页的纸面尺寸。页号可能是 0（早期数据），渲染会失败，
                    # 所以依次试到第一个能渲染的页；全试不通就按 A4 兜底 —— 不能因为
                    # 这里取不到尺寸就退化成平台几何，那样整章的页眉页脚抑制都会失效。
                    _probe = _pages[0] if _pages else None
                    try:
                        if _probe is None and _page_nos:
                            import io as _io
                            import pdfplumber as _pdfplumber
                            with _pdfplumber.open(_io.BytesIO(pdf_source)) as _pdf:
                                _page = _pdf.pages[int(_page_nos[0])]
                                _probe = {"width_pt": float(_page.width), "height_pt": float(_page.height)}
                        if _probe is not None:
                            _pdf_geom = {
                                "page_width": int(float(_probe["width_pt"]) * 12700),
                                "page_height": int(float(_probe["height_pt"]) * 12700),
                                "top_margin": 0, "bottom_margin": 0,
                                "left_margin": 0, "right_margin": 0,
                                "header_distance": 0, "footer_distance": 0,
                            }
                    except Exception:  # noqa: BLE001
                        _pdf_geom = {}
                    if not _pdf_geom:
                        _pdf_geom = {
                            "page_width": 7560310, "page_height": 10692130,
                            "top_margin": 0, "bottom_margin": 0,
                            "left_margin": 0, "right_margin": 0,
                            "header_distance": 0, "footer_distance": 0,
                        }
                _ensure_section("pdf")
                if _page_nos:
                    # 逐页分流：有填写位的页重建成真文字（客户能填），其余页照旧位图。
                    # 格式章的第一节由分节符开页，之后的每一节都要自己分页。
                    _paras = _append_pdf_pages(doc, pdf_source, _page_nos, _heading,
                                               toc=toc, level=_level,
                                               new_page=_pdf_pages_started[0])
                    if _paras:
                        _pdf_pages_started[0] = True
                        _used_pages.update(_page_nos)
                else:
                    _paras = None
                if _paras:
                    _last_pdf_page = _paras[-1]
                    continue
            elif _last_pdf_page is not None and toc is not None:
                # 这一节的标题落在上一节那一页上（父子小节共用一页），本页已落过图。
                # 不再单独出页，只把目录条目记到那一页上：页码与原件一致。
                toc.add(_last_pdf_page, _heading, _level)
                continue
        # 标题为空的章只写编号、不登记目录（否则目录里会多一条孤零零的「4.7」）
        _ensure_section("platform")
        _paragraph = _add_export_heading(doc, _text, _level, toc=toc, record=bool(_title.strip()))
        # 表单章（投标函/开标一览表/无违法记录声明等）也属于一级章：同样从新页开始。
        if _level == 1:
            if _platform_started[0]:
                _paragraph.paragraph_format.page_break_before = True
            _platform_started[0] = True
        # Render-time only: title removal, numbering and Word heading depth must
        # share one outline-aware pass. Normalizing before title removal creates
        # invisible parent numbers when the body repeats its chapter title.
        _md_render_chapter(doc, str(r["content"] or ""), r["level"], r["title"], images,
                           chapter_number=str(_num), container=str(_num) in _containers,
                           toc=toc)
    written = 0
    if anchor is not None:
        written = _add_toc_field(doc, anchor._p, toc.entries if toc else [], int(toc_level))
    try:
        # 全篇写完之后再按**最终**的分节几何夹一遍制表位。
        # 逐章插入时夹的是「当时最后一节」的版心，而后面每换一次章节类型
        # （原件章 ↔ 平台章 ↔ PDF 章）都会再插一个分节符，前面章节的版心可能随之改变；
        # 目录条目也是收尾才写进去的。只有这一遍的结果才是交付文件里真正生效的值。
        _clamp_tab_stops(doc)
    except Exception:  # noqa: BLE001 —— 收尾夹制表位失败不影响出稿
        pass
    try:
        _autofill_known_fields(doc, source_facts)
        _canon_project_names(doc)   # 名称口径统一：变体改回封面上的官方名称
        _ensure_bid_letter_salutation(doc)   # 投标函缺「致：…」时补上
    except Exception:  # noqa: BLE001 —— 名称规范化失败不影响出稿
        pass
    return written
