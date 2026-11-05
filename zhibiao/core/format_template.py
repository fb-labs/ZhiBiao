# -*- coding: utf-8 -*-
"""按 Word 招标文件的版式，把「投标文件格式」章的每一节原样切出来给成稿用。

为什么不是"重新排一遍"：格式章的字体、字号、表格合并、行高、分页位置都是招标人规定的，
评审也照着看。只要我们自己拿 python-docx 重画一遍，就有无穷多处会和原件不一样。所以这里
**只做搬运**：把原件的 XML 元素整段 deepcopy 过去，唯一允许的改动是往空位里插字。

两个必须处理的现实问题：

1. 样式是继承来的。原件里大多数 run 根本没有 `w:sz`，字号来自 `python-docx` 里那套
   样式链（例如「无间隔1」→ Normal）。搬到别的文档后样式表不同，字就会变大小。所以搬运前
   先把**继承格式物化成直接格式**（见 `materialize_styles`），外观不再依赖目标文档的样式表。
2. 章开头常有一份"小节目录"，标题和正文小节重名。切分必须认正文那一处（见
   `_is_real_section`），否则切出来的是目录里的一行字。
"""

from __future__ import annotations

import copy
import hashlib
import io
import re
import warnings
from typing import Any

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

# 章标题必须提到这些词之一（招标文件里这一章的叫法就这几种）
_TEMPLATE_TITLE = re.compile(r"(投标文件格式|响应文件格式|投标文件组成)")
# 章标题形如「第六章 投标文件格式」——也允许「第六章：投标文件格式」
_CHAPTER_HEAD = re.compile(r"^\s*第[一二三四五六七八九十百]+[章节部分]")
# 小节标题：「一、投标函」/「（二）身份证明」/「1. 投标函」
_SECTION_HEAD = re.compile(r"^\s*(?:[一二三四五六七八九十]{1,3}\s*[、.．)]|[（(][一二三四五六七八九十\d]{1,3}[）)])")
_UNDERLINE = re.compile(r"[_＿]{3,}|…{3,}")
_EMPTY_PAREN = re.compile(r"[（(][\s\u3000]{0,10}[）)]")
_NUM_PREFIX = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+[章节部分]|[一二三四五六七八九十百]{1,3}\s*[、.．)]|"
    r"[（(][一二三四五六七八九十百\d]{1,3}[）)]|\d{1,2}(?:\.\d{1,2})*\s*[、.．)]?)\s*")
_SPACE = re.compile(r"[\s\u3000]+")
_PUNCT = re.compile(r"[。，、；：.,;:（）()【】\[\]“”\"'’‘]")

# 切出来的切片按 (原件 sha1, 小节标题) 缓存：导出一次会连着切十几节，
# 每节都重新解析 2MB 的 docx 会把一次导出拖到十几秒。
# 切片里带版本水印：样式表随切片走是从这一版开始的，老切片出稿会"内容对、位置错"。
SLICE_VERSION = "zbt-fmt-2"
_SLICE_CACHE: dict[tuple[str, str], bytes] = {}
# 老格式（.doc/.wps/.rtf）不是 zip，python-docx 打不开。先用 LibreOffice 转成 docx，
# 转出来的字节按内容哈希缓存——同一份招标文件一次上传会切十几节，不能每节转一遍。
_CONVERT_CACHE: dict[str, bytes] = {}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"


def _as_docx(raw: bytes) -> bytes:
    """拿到可被 python-docx 打开的 docx 字节；转不了就原样返回（调用方按失败处理）。"""
    data = bytes(raw or b"")
    if not data:
        return data
    if data[:2] == b"PK":
        return data
    if not data.startswith(_OLE_MAGIC):
        return data
    key = hashlib.sha1(data[:2_000_000] + str(len(data)).encode()).hexdigest()[:16]
    cached = _CONVERT_CACHE.get(key)
    if cached:
        return cached
    try:
        import os
        import shutil
        import subprocess
        import tempfile

        soffice = shutil.which("soffice")
        if not soffice:
            return data
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src.doc")
            with open(src, "wb") as fh:
                fh.write(data)
            subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", td, src],
                           capture_output=True, timeout=300)
            out = os.path.join(td, "src.docx")
            if not os.path.exists(out):
                return data
            with open(out, "rb") as fh:
                converted = fh.read()
    except Exception:  # noqa: BLE001 —— 转不了就当没有版式，绝不拦住上传/导出
        return data
    while len(_CONVERT_CACHE) > 4:
        _CONVERT_CACHE.pop(next(iter(_CONVERT_CACHE)), None)
    _CONVERT_CACHE[key] = converted
    return converted
_SLICE_LIMIT = 32

# 解析结果缓存：一次上传/导出要切 10~20 节，每节都重新解压 docx 是纯浪费
# （实测单次解析 180ms，20 节就是 3.6 秒）。只缓存最近几份，避免吃内存。
_DOC_CACHE: dict[str, tuple[Any, list]] = {}
_DOC_LIMIT = 4


def _raw_key(raw: bytes) -> str:
    return hashlib.sha1(bytes(raw)[:4_000_000] + str(len(raw)).encode()).hexdigest()[:16]


def _open(raw: bytes) -> Any:
    """解析成 python-docx 文档，顺手把符号写法的正文（`w:sym`）还原成 `w:t`。

    `_as_docx` 只负责把 .doc 转成 docx；转换结果里"符号字体"那些字仍然是 `w:sym`。
    这里统一还原一次，后面所有按段落取文字的逻辑（`_text`、空位识别、切片）才看得见。
    """
    document = docx.Document(io.BytesIO(_as_docx(bytes(raw))))
    normalize_symbols(document.element.body)
    return document


def _load(raw: bytes) -> tuple[Any, str, list] | None:
    """解析一次并缓存 (document, 章标题, 小节列表)。"""
    key = _raw_key(raw)
    hit = _DOC_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        # .doc/.wps/.rtf 不是 zip：这里先转成 docx 再解析（同一份文件只转一次）
        document = _open(bytes(raw))
    except Exception:  # noqa: BLE001
        return None
    scope, sections = _sections(document)
    value = (document, scope, sections)
    if len(_DOC_CACHE) >= _DOC_LIMIT:
        _DOC_CACHE.pop(next(iter(_DOC_CACHE)), None)
    _DOC_CACHE[key] = value
    return value


# ───────────────────────── 基础遍历 ─────────────────────────

def _body_children(document: Any) -> list[Any]:
    """body 下真正的元素（含最后的 w:sectPr，它是文档节属性，不是内容）。"""
    return [el for el in document.element.body if isinstance(el.tag, str)]


def _is_para(el: Any) -> bool:
    return el.tag == qn("w:p")


def _is_table(el: Any) -> bool:
    return el.tag == qn("w:tbl")


# 真正的符号字体：这些字体里的 `w:sym` 是图形符号（项目符号、对勾…），不能当文字搬
_SYMBOL_FONT = re.compile(
    r"(?:^|[^a-z])(?:symbol|wingdings?|webdings?|marlett|dingbats?|"
    r"monotype\s+sorts|mt\s+extra)(?:[^a-z]|$)", re.I)
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# 「符号槽位码位 + 普通字体」在 Word 里是纯空白（实测墨迹为 0）：留个字位占位，别留空
_SYMBOL_PLACEHOLDER = "□"


def _symbol_text(node: Any) -> str | None:
    """`w:sym` 该落成的文字；返回 None 表示这个符号节点要原样保留。

    为什么会有 `w:sym`：`.doc` 里有些字存的是"字符码"，LibreOffice 转 docx 时不会把它
    落成 `w:t`，而是写成 `<w:sym w:font="Times New Roman" w:char="6307"/>`。这些字 Word
    能显示，但所有只遍历 `w:t` 的取文字逻辑（`_text` / `_slice_text` / `docx` 的
    `p.text`）一律读不到——「（9）投标设备技术性能指标的详细描述；」在切片里只剩前半句
    就是这个原因。

    实测（Word 2016 渲染成 PDF 后用文字层 + 像素墨迹双重核对）：

    * 普通字体 + 普通码位（事故原样）：Word 正常显示该字 → 落成 `w:t` 无损；
    * 真符号字体 + 符号槽位码位（`F0A7` / `F0B7` / `F0FC`）：Word 正常画出字形
      （▪ / • / ✓）→ **原样保留**，换成文字反而会毁掉项目符号；
    * 真符号字体 + 普通码位：Word 会画出别的字形，但那是符号字体自己的编码，
      不敢替它解释 → 原样保留；
    * 普通字体 + 符号槽位码位：Word 画不出任何东西（实测墨迹为 0，纯空白）→
      换成占位符 `□`，至少让读者知道"这儿原本有个字"。
    """
    raw = str(node.get(qn("w:char")) or "").strip()
    if not raw:
        return None
    try:
        code = int(raw, 16)
    except ValueError:
        return None
    symbol_font = bool(_SYMBOL_FONT.search(str(node.get(qn("w:font")) or "")))
    if code < 0x20 or 0x7F <= code < 0xA0:            # 控制字符：不是文字
        return None
    if 0xD800 <= code <= 0xDFFF:                      # 代理区
        return None
    if 0xE000 <= code <= 0xF8FF or code >= 0xF0000:   # 私有区 / 补充私有区：字形槽位
        return None if symbol_font else _SYMBOL_PLACEHOLDER
    if symbol_font:                                   # 符号字体里的普通码位：交给 Word
        return None
    try:
        return chr(code)
    except (ValueError, OverflowError):
        return None


def _text(el: Any) -> str:
    """元素里的可见文字：`w:t` + `w:sym`（符号写法里装的也是正文文字）。"""
    out: list[str] = []
    for node in el.iter():
        if node.tag == qn("w:t"):
            out.append(node.text or "")
        elif node.tag == qn("w:sym"):
            text = _symbol_text(node)
            if text:
                out.append(text)
    return "".join(out)


def element_text(el: Any) -> str:
    """给别的模块共用的取文字口径（切片转纯文本、表格单元格都走这里）。"""
    return _text(el)


def normalize_symbols(element: Any) -> int:
    """把能还原的 `w:sym` 落成 `w:t`，返回还原的字数。

    **只改文字的存法，不动格式**：run 的 `w:rPr`（字体/字号/下划线/颜色）原样保留，
    只是把读不出来的 `<w:sym>` 换成 `<w:t>`；连续的可还原符号合并成一个 `w:t`，
    与 `w:t` 的相对顺序不变。真符号（Wingdings 之类）一个都不碰。
    """
    changed = 0
    for run in list(element.iter(qn("w:r"))):
        if not run.findall(qn("w:sym")):
            continue
        rebuilt: list[Any] = []
        pending: list[str] = []

        def _flush() -> None:
            if not pending:
                return
            node = OxmlElement("w:t")
            node.set(_XML_SPACE, "preserve")
            node.text = "".join(pending)
            pending.clear()
            rebuilt.append(node)

        for child in list(run):
            if child.tag != qn("w:sym"):
                _flush()
                rebuilt.append(child)
                continue
            text = _symbol_text(child)
            if text is None:
                _flush()
                rebuilt.append(child)      # 真符号：原样留着
                continue
            pending.append(text)
            changed += 1
        _flush()
        for child in list(run):
            run.remove(child)
        for child in rebuilt:
            run.append(child)
    return changed


def _clean(text: str) -> str:
    return _SPACE.sub("", _PUNCT.sub("", _NUM_PREFIX.sub("", str(text or "").strip())))


def normalize_title(text: str) -> str:
    return _clean(text)


# ───────────────────────── 样式物化 ─────────────────────────

def _style_chain(style: Any):
    seen = 0
    while style is not None and seen < 12:
        yield style
        style = getattr(style, "base_style", None)
        seen += 1


def _first(style_obj: Any, attr: str) -> Any:
    for style in _style_chain(style_obj):
        try:
            value = getattr(style, attr, None)
        except Exception:  # noqa: BLE001
            value = None
        if value is not None:
            return value
    return None


def _materialize_paragraph(paragraph: Any) -> bool:
    """把段落/run 里继承自样式的格式写成直接格式，返回是否改动。"""
    changed = False
    style = paragraph.style
    fmt = paragraph.paragraph_format
    try:
        if fmt.alignment is None:
            align = _first(style, "paragraph_format") and _first(style, "paragraph_format").alignment
            if align is not None:
                fmt.alignment = align
                changed = True
        for attr in ("left_indent", "first_line_indent", "space_before", "space_after",
                     "line_spacing", "page_break_before"):
            if getattr(fmt, attr, None) is None:
                style_fmt = getattr(style, "paragraph_format", None) if style else None
                value = getattr(style_fmt, attr, None) if style_fmt is not None else None
                if value is None:
                    for base in _style_chain(style):
                        base_fmt = getattr(base, "paragraph_format", None)
                        value = getattr(base_fmt, attr, None) if base_fmt is not None else None
                        if value is not None:
                            break
                if value is not None:
                    try:
                        setattr(fmt, attr, value)
                        changed = True
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass

    # 大纲级别：样式里带 outlineLvl 时要落成直接属性，否则搬走后"能不能进目录"就变了
    ppr = paragraph._p.get_or_add_pPr()
    if ppr.find(qn("w:outlineLvl")) is None:
        for base in _style_chain(style):
            el = getattr(base, "element", None)
            if el is None:
                continue
            base_ppr = el.find(qn("w:pPr"))
            lvl = base_ppr.find(qn("w:outlineLvl")) if base_ppr is not None else None
            if lvl is not None:
                new = OxmlElement("w:outlineLvl")
                new.set(qn("w:val"), lvl.get(qn("w:val")) or "0")
                ppr.append(new)
                changed = True
                break

    for run in paragraph.runs:
        rpr = run._element.get_or_add_rPr()
        rstyle = run.style if run.style is not None else style
        if run.font.size is None:
            size = _first(rstyle, "font") and _first(rstyle, "font").size
            if size is not None:
                run.font.size = size
                changed = True
        if run.font.bold is None:
            bold = _first(rstyle, "font") and _first(rstyle, "font").bold
            if bold is not None:
                run.font.bold = bold
                changed = True
        if run.font.name is None:
            font = _first(rstyle, "font")
            name = getattr(font, "name", None) if font is not None else None
            if name:
                run.font.name = name
                changed = True
        # 中文字体（eastAsia）与西文字体分开：只设 w:ascii 时中文会退回默认宋体
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        if fonts.get(qn("w:eastAsia")) is None:
            for base in _style_chain(rstyle):
                el = getattr(base, "element", None)
                if el is None:
                    continue
                base_rpr = el.find(qn("w:rPr"))
                base_fonts = base_rpr.find(qn("w:rFonts")) if base_rpr is not None else None
                if base_fonts is not None and base_fonts.get(qn("w:eastAsia")):
                    fonts.set(qn("w:eastAsia"), base_fonts.get(qn("w:eastAsia")))
                    changed = True
                    break
        if fonts.get(qn("w:eastAsia")) is None and fonts.get(qn("w:ascii")):
            # 样式只给了西文字体：中文沿用同一个（比继承成宋体更接近原样）
            fonts.set(qn("w:eastAsia"), fonts.get(qn("w:ascii")))
            changed = True
    return changed


def materialize_styles(document: Any) -> int:
    """整篇把继承格式物化成直接格式，返回处理的段落数。"""
    count = 0
    for el in document.element.body.iter(qn("w:p")):
        try:
            if _materialize_paragraph(Paragraph(el, document._body)):
                count += 1
        except Exception:  # noqa: BLE001 —— 单个段落物化失败不影响整篇
            continue
    return count


# ───────────────────────── 章 / 小节定位 ─────────────────────────

def _find_scope(document: Any) -> tuple[str, int, int]:
    """返回 (章标题, 起始元素下标, 结束元素下标)；没找到返回 ("", -1, -1)。

    不要"第一个含『投标文件格式』的短段落"——投标人须知里常有一条清单
    「（6）投标文件格式；」，那是个列表项，不是章。所以对每个候选都试算一次它后面
    能切出几个**真正的**小节，取小节最多的那个（并列取靠后的，正文章在后）。
    """
    children = _body_children(document)
    best: tuple[str, int, int] = ("", -1, -1)
    best_count = 0
    for idx, el in enumerate(children):
        if not _is_para(el):
            continue
        text = _text(el).strip()
        if not text or len(text) > 60 or not _TEMPLATE_TITLE.search(text):
            continue
        # 列表项/句子尾巴一律不算章标题
        if re.search(r"[；;，,。]$", text):
            continue
        if not (_CHAPTER_HEAD.match(text) or (len(text) <= 30 and not text.startswith(("（", "(")))):
            continue
        end = len(children)
        for j in range(idx + 1, len(children)):
            other = children[j]
            if not _is_para(other):
                continue
            other_text = _text(other).strip()
            if (_CHAPTER_HEAD.match(other_text) and len(other_text) <= 60
                    and not _TEMPLATE_TITLE.search(other_text)):
                end = j
                break
        candidates = _section_candidates(children, idx, end)
        real = 0
        for pos, (sec_idx, _title, level) in enumerate(candidates):
            next_idx = candidates[pos + 1][0] if pos + 1 < len(candidates) else end
            next_level = candidates[pos + 1][2] if pos + 1 < len(candidates) else 0
            if _is_real_section(children, sec_idx, next_idx, level, next_level):
                real += 1
        if real >= 2 and real >= best_count:
            best, best_count = (text, idx, end), real
    return best


def _section_level(text: str) -> int:
    """小节层级：0 = 不是小节标题，1 = 一、，2 = （一）。阿拉伯编号的括号项（如「（9）…」）
    通常是正文里的清单项而不是小节，一律不当小节。"""
    value = str(text or "").strip()
    if re.match(r"^[一二三四五六七八九十]{1,3}\s*[、.．)]", value):
        return 1
    if re.match(r"^[（(][一二三四五六七八九十]{1,3}[）)]", value):
        return 2
    return 0


def _section_candidates(children: list[Any], start: int, end: int) -> list[tuple[int, str, int]]:
    found: list[tuple[int, str, int]] = []
    for idx in range(start + 1, end):
        el = children[idx]
        if not _is_para(el):
            continue
        text = _text(el).strip()
        if not text or len(text) > 40:
            continue
        level = _section_level(text)
        if not level:
            continue
        if re.search(r"[。；;]$", text):
            continue
        found.append((idx, text, level))
    return found


def _is_real_section(children: list[Any], idx: int, next_idx: int, level: int,
                     next_level: int) -> bool:
    """目录条目后面紧接着下一个标题；正文小节后面有成段内容，或者紧跟着更细一级的小节。

    后者是「八、资格审查资料」这种父小节：它自己下面没有正文段落，直接就是（一）（二）…，
    但它是真的小节，必须留下——平台成稿里这些父章也是要落版的。
    """
    if next_level == level + 1:
        return True
    for j in range(idx + 1, next_idx):
        el = children[j]
        if _is_table(el):
            return True
        if _is_para(el) and _text(el).strip():
            return True
    return False


def _has_content(children: list[Any], idx: int, next_idx: int) -> bool:
    for j in range(idx + 1, next_idx):
        el = children[j]
        if _is_table(el):
            return True
        if _is_para(el) and _text(el).strip():
            return True
    return False


def _sections(document: Any) -> tuple[str, list[tuple[int, int, str]]]:
    """切出格式章的小节。

    章首那份"小节目录"是连续的一串标题、彼此之间没有正文；正文第一个小节之后才开始
    有内容。所以判定很简单：**第一个后面有内容的小节之前，全是目录条目**；它之后的
    每一个标题都是真小节——包括没写正文、只占一行的技术类小节（招标文件只给白纸，
    平台本来就要按项目实际写，但小节本身必须留在成稿里）。
    """
    scope, start, end = _find_scope(document)
    if start < 0:
        return "", []
    children = _body_children(document)
    candidates = _section_candidates(children, start, end)
    if not candidates:
        return scope, []
    spans: list[tuple[int, int, str]] = []
    for pos, (idx, title, _level) in enumerate(candidates):
        next_idx = candidates[pos + 1][0] if pos + 1 < len(candidates) else end
        spans.append((idx, next_idx, title))
    first_real = 0
    for pos, (idx, next_idx, _title) in enumerate(spans):
        if _has_content(children, idx, next_idx):
            first_real = pos
            break
    else:
        first_real = len(spans)
    real = spans[first_real:]
    # 章首目录里的标题与正文小节逐字相同，按原始文字去重即可。不能按"去掉编号后"的
    # 文字去重：「七、分项报价表」和「（二）分项报价表」去掉编号后是同一个词。
    dedup: dict[str, tuple[int, int, str]] = {}
    for item in real:
        dedup[item[2].strip()] = item
    return scope, list(dedup.values())


# ───────────────────────── 空位识别 ─────────────────────────

def _blanks(document: Any) -> list[dict[str, Any]]:
    blanks: list[dict[str, Any]] = []
    for t_idx, table in enumerate(document.tables):
        for r_idx, row in enumerate(table.rows):
            for c_idx, cell in enumerate(row.cells):
                if str(cell.text or "").strip():
                    continue
                label = ""
                if r_idx > 0:
                    label = str(table.rows[r_idx - 1].cells[c_idx].text or "").strip()[:20]
                if not label and c_idx > 0:
                    label = str(row.cells[c_idx - 1].text or "").strip()[:20]
                blanks.append({"id": f"table{t_idx}:r{r_idx}:c{c_idx}", "kind": "cell",
                               "label": label, "expect": "text"})
    for p_idx, paragraph in enumerate(document.paragraphs):
        text = paragraph.text
        for n, _match in enumerate(_UNDERLINE.finditer(text), 1):
            blanks.append({"id": f"p{p_idx}:u{n}", "kind": "underline", "label": "", "expect": "text"})
        for n, _match in enumerate(_EMPTY_PAREN.finditer(text), 1):
            blanks.append({"id": f"p{p_idx}:b{n}", "kind": "bracket", "label": "", "expect": "text"})
    return blanks


# ───────────────────────── 对外 API ─────────────────────────

def detect(raw: bytes, filename: str = "") -> dict:
    """这份招标文件里有没有「投标文件格式」章，以及这一章有哪些小节。"""
    try:
        document = _open(bytes(raw or b""))
    except Exception as exc:  # noqa: BLE001
        return {"has_template": False, "kind": "", "scope_title": "", "section_titles": [],
                "reason": f"不是可解析的 docx：{exc.__class__.__name__}"}
    scope, sections = _sections(document)
    if not scope:
        return {"has_template": False, "kind": "", "scope_title": "", "section_titles": [],
                "reason": "没有找到标题含「投标文件格式」的章"}
    if len(sections) < 2:
        return {"has_template": False, "kind": "", "scope_title": scope,
                "section_titles": [s[2] for s in sections],
                "reason": "格式章里不足 2 个可切分的小节"}
    return {"has_template": True, "kind": "docx", "scope_title": scope,
            "section_titles": [s[2] for s in sections], "reason": ""}


def extract_sections(raw: bytes, filename: str = "") -> list[dict]:
    """列出格式章里的小节（标题、归一化键、空位数量、内部显式分页数）。"""
    loaded = _load(bytes(raw or b""))
    if loaded is None:
        return []
    document, scope, sections = loaded
    if not sections:
        return []
    children = _body_children(document)
    out: list[dict] = []
    for index, (start, end, title) in enumerate(sections):
        slice_els = [el for el in children[start:end] if isinstance(el.tag, str)]
        breaks = 0
        for el in slice_els:
            breaks += len(el.findall(".//" + qn("w:br") + f"[@{qn('w:type')}='page']"))
            breaks += len(el.findall(".//" + qn("w:pageBreakBefore")))
        out.append({
            "title": title,
            "match_key": _clean(title),
            "scope": scope,
            "index": index,
            "page_breaks": breaks,
            "blanks": _blanks_of_elements(document, slice_els),
        })
    return out


def _blanks_of_elements(document: Any, elements: list[Any]) -> list[dict[str, Any]]:
    """只统计这一段里的空位（表格空单元格按下标编号）。"""
    blanks: list[dict[str, Any]] = []
    table_no = 0
    para_no = 0
    for el in elements:
        if _is_table(el):
            for r_idx, tr in enumerate(el.findall(qn("w:tr"))):
                cells = tr.findall(qn("w:tc"))
                for c_idx, tc in enumerate(cells):
                    if _text(tc).strip():
                        continue
                    blanks.append({"id": f"table{table_no}:r{r_idx}:c{c_idx}", "kind": "cell",
                                   "label": "", "expect": "text"})
            table_no += 1
        elif _is_para(el):
            text = _text(el)
            for n, _m in enumerate(_UNDERLINE.finditer(text), 1):
                blanks.append({"id": f"p{para_no}:u{n}", "kind": "underline",
                               "label": "", "expect": "text"})
            for n, _m in enumerate(_EMPTY_PAREN.finditer(text), 1):
                blanks.append({"id": f"p{para_no}:b{n}", "kind": "bracket",
                               "label": "", "expect": "text"})
            para_no += 1
    return blanks


def page_setup(raw: bytes) -> dict[str, Any]:
    """招标原件的页面设置：纸张、页边距、纸张方向（EMU）。

    成稿默认是 python-docx 的 Letter（21.59×27.94cm），而国内招标文件几乎都是 A4
    （21.00×29.70cm）。就差 6 毫米页宽，绝对制表位（「投标人：____（单位公章）」那种）、
    表格列宽、分页位置会一起错位——所以这一段必须按原件来。
    """
    loaded = _load(bytes(raw or b""))
    if loaded is None:
        return {}
    document = loaded[0]
    try:
        section = document.sections[-1]
    except Exception:  # noqa: BLE001
        return {}
    setup: dict[str, Any] = {}
    for name in ("page_width", "page_height", "top_margin", "bottom_margin",
                 "left_margin", "right_margin", "header_distance", "footer_distance"):
        value = getattr(section, name, None)
        if value:
            setup[name] = int(value)
    orientation = getattr(section, "orientation", None)
    if orientation is not None:
        setup["orientation"] = int(orientation)
    return setup


def slice_page_setup(blob: bytes) -> dict[str, Any]:
    """读切片自带的页面设置（`extract_section_docx` 把原件的纸张几何封在切片里）。"""
    try:
        document = docx.Document(io.BytesIO(bytes(blob)))
        section = document.sections[-1]
    except Exception:  # noqa: BLE001
        return {}
    setup: dict[str, Any] = {}
    for name in ("page_width", "page_height", "top_margin", "bottom_margin",
                 "left_margin", "right_margin", "header_distance", "footer_distance"):
        value = getattr(section, name, None)
        if value:
            setup[name] = int(value)
    orientation = getattr(section, "orientation", None)
    if orientation is not None:
        setup["orientation"] = int(orientation)
    return setup


def extract_section_docx(raw: bytes, title: str, *, index: int | None = None) -> bytes | None:
    """切出一个小节，返回独立 docx 字节；第一个 body 元素就是该小节的标题段。

    `index` 是 `extract_sections` 返回列表里的位次，**优先用它定位**：招标文件里
    「七、分项报价表」和「（二）分项报价表」去掉编号后是同一个词，只按标题匹配会
    把两个小节指到同一段内容上——一个重复、一个丢失（实测少 6 张表、100 多段）。
    """
    if not raw or (index is None and not str(title or "").strip()):
        return None
    key = (_raw_key(raw), _clean(title), index)
    cached = _SLICE_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = _load(bytes(raw))
    if loaded is None:
        return None
    document, _scope, sections = loaded
    if not sections:
        return None
    target = None
    wanted = _clean(title)
    if index is not None and 0 <= int(index) < len(sections):
        target = sections[int(index)][:2]
    else:
        for start, end, name in sections:
            if _clean(name) == wanted:
                target = (start, end)
                break
    if target is None:
        return None
    children = _body_children(document)
    picked = [el for el in children[target[0]:target[1]] if isinstance(el.tag, str)]
    # 原件的换页点常常挂在"下一小节标题段"上（Word 里分节符属于它前面那一节）。
    # 只看本节范围就会把这一处换页弄丢：一节少一页，几十节下来整本薄一大截。
    if picked and target[1] < len(children):
        nxt = children[target[1]]
        if _has_sectpr(nxt) and getattr(picked[-1], "tag", None) == qn("w:p"):
            _insert_page_break(picked[-1])
    # 本节第一段自己带分节符时，换页应发生在本节之前（标题落新页首），而不是之后
    if picked and _has_sectpr(picked[0]):
        _page_break_before(picked[0])
    # 只物化这一段里的段落：整篇 1100+ 段物化要 9 秒（实测），而一次导出只用到十来个
    # 小节，做全量纯属浪费——这也是这条路径能在导出请求里同步跑完的前提。
    for el in picked:
        for p_el in el.iter(qn("w:p")):
            try:
                _materialize_paragraph(Paragraph(p_el, document._body))
            except Exception:  # noqa: BLE001
                continue
    # 段落里带的 sectPr 属于原文档的节属性，搬过去会把目标文档的页面设置顶掉
    # 切片直接**从原件裁出来**：样式表、编号、字体表、主题原封不动跟着走。
    # 早先是新建一个空 docx 再把段落搬进去，样式表丢了，段落上的 w:pStyle 就成了空引用，
    # Word/LibreOffice 一律退回 Normal —— 缩进没了（「投标人：（单位公章）」跑到最左边）、
    # 表格边框没了、行距也不对。这些都是原件里写着的，不能丢。
    # 符号写法（`w:sym`）里的正文必须落成 `w:t` 再进切片：Word 自己能显示它，但平台的
    # 取文字逻辑只认 `w:t`（`_text` / `_slice_text` / `docx` 的 `p.text` 一律读不到）——
    # 「（9）投标设备技术性能指标的详细描述；」取出来只剩前半句就是这么来的。落成 `w:t`
    # 后显示效果不变（Word 实测渲染同一句中文），取文字、字数、原文对照才都看得见。
    # 转换只改存法，run 的 rPr 一个字节不动。
    out_doc = _open(raw)
    body = out_doc.element.body
    kids = _body_children(out_doc)
    for index, el in enumerate(kids):
        if el.tag == qn("w:sectPr"):      # 文档级节属性要留着
            continue
        if not (target[0] <= index < target[1]):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    kept = [el for el in _body_children(out_doc) if el.tag != qn("w:sectPr")]
    # 原件的换页点常常挂在"下一小节标题段"上（Word 里分节符属于它前面那一节）。
    # 只看本节范围就会把这一处换页弄丢：一节少一页，几十节下来整本薄一大截。
    if kept and target[1] < len(kids):
        nxt = kids[target[1]]
        if _has_sectpr(nxt) and kept[-1].tag == qn("w:p"):
            _insert_page_break(kept[-1])
    # 本节第一段自己带分节符时，换页应发生在本节之前（标题落新页首），而不是之后
    if kept and _has_sectpr(kept[0]):
        _page_break_before(kept[0])
    for el in kept:
        _sanitize_sectpr(el)
    _prune_parts(out_doc)
    try:
        # 版本水印：样式表跟着切片走是从这一版开始的。库里的老切片没有样式，
        # 用它出稿会"内容对、位置全错"，导出侧据此判为过期并回落到文本渲染。
        out_doc.core_properties.comments = SLICE_VERSION
    except Exception:  # noqa: BLE001
        pass
    buf = io.BytesIO()
    with warnings.catch_warnings():
        # python-docx 改 core_properties 时会写两次 core.xml（下面 _dedupe_zip 洗掉），
        # 这条警告每切一节刷一行日志，没有信息量
        warnings.simplefilter("ignore")
        out_doc.save(buf)
    blob = _dedupe_zip(buf.getvalue())
    if len(_SLICE_CACHE) >= _SLICE_LIMIT:
        _SLICE_CACHE.pop(next(iter(_SLICE_CACHE)), None)
    _SLICE_CACHE[key] = blob
    return blob


def _dedupe_zip(blob: bytes) -> bytes:
    """重写 zip，同名部件只留最后一份。

    python-docx 给载入的文档改 `core_properties` 时会把 `docProps/core.xml` 写两遍。
    重复条目在严格读取器上可能拿到旧的那份（版本水印就白写了），顺手洗一遍。
    """
    import zipfile

    try:
        source = zipfile.ZipFile(io.BytesIO(blob))
        parts: dict[str, bytes] = {}
        for info in source.infolist():
            if info.is_dir():
                continue
            parts[info.filename] = source.read(info.filename)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
            for name, data in parts.items():
                target.writestr(name, data)
        return out.getvalue()
    except Exception:  # noqa: BLE001 —— 洗不了就用原样，不能因此切不出切片
        return blob


def _prune_parts(document: Any) -> None:
    """切片只留用得上的部件：没被引用的图片、页眉页脚一律摘掉。

    原件里常带整本宣传册级别的图片（实测单个切片 1.6MB，22 个切片 35MB），
    而一个小节其实用不到它们；页眉页脚更是每节都要换的内容。丢掉之后切片约
    200KB，写库和导出都不再拖泥带水。
    """
    try:
        rels = document.part.rels
    except Exception:  # noqa: BLE001
        return
    used: set[str] = set()
    for el in document.element.body.iter():
        for attr in ("r:embed", "r:id", "r:link", "r:pict"):
            value = el.get(qn(attr))
            if value:
                used.add(str(value))
    for r_id, rel in list(rels.items()):
        reltype = str(getattr(rel, "reltype", ""))
        if reltype.endswith(("/image", "/header", "/footer")) and r_id not in used:
            try:
                document.part.drop_rel(r_id)
            except Exception:  # noqa: BLE001
                continue


def _insert_page_break(element: Any) -> bool:
    """在段落末尾补一个分页符（等同"分节符（下一页）"的换页效果），已有则不重复加。"""
    try:
        for br in element.iter(qn("w:br")):
            if br.get(qn("w:type")) == "page":
                return False
        run = OxmlElement("w:r")
        br = OxmlElement("w:br")
        br.set(qn("w:type"), "page")
        run.append(br)
        element.append(run)
        return True
    except Exception:  # noqa: BLE001
        return False


def _has_sectpr(element: Any) -> bool:
    """这个段落是不是带着分节符（原件的换页点）。"""
    if element is None or getattr(element, "tag", None) != qn("w:p"):
        return False
    ppr = element.find(qn("w:pPr"))
    return ppr is not None and ppr.find(qn("w:sectPr")) is not None


def _page_break_before(element: Any) -> None:
    """让这一段从新页开始（分段符在上一段末尾时，用它把换页效果搬过来）。"""
    ppr = element.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        element.insert(0, ppr)
    if ppr.find(qn("w:pageBreakBefore")) is None:
        ppr.insert(0, OxmlElement("w:pageBreakBefore"))


def _strip_sectpr_keep_break(element: Any) -> None:
    """搬进成稿时摘掉节属性（纸张由成稿自己定），但把换页留下——不换页就是漏页。"""
    for ppr in element.iter(qn("w:pPr")):
        sect = ppr.find(qn("w:sectPr"))
        if sect is not None:
            _insert_page_break(element)
            ppr.remove(sect)


def _sanitize_sectpr(element: Any) -> bool:
    """保留段落里的分节符，只摘掉会污染成稿的部分，返回是否该整段丢弃。

    招标文件几乎都拿"分节符（下一页/奇数页）"当分页用，节属性里还写着它自己的纸张与
    页边距。原来的做法是把节属性整段删掉，等于把分页也删了（35 页的格式章塌成 20 页）。
    现在原样保留分节符，只去掉三样东西：

    * 页眉页脚引用（r:id 指向原文件的部件，搬过去就是断链）；
    * 页码重新起算（会把成稿前面几章的页码打乱）；
    * 封面不同的设置（titlePg）。

    只有"纯 sectPr 元素"（body 级文档节属性）才返回 True 让调用方丢弃它——那份纸张
    由切片自己的 body sectPr 统一接管。
    """
    if element.tag == qn("w:sectPr"):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
        return True
    for sect in element.iter(qn("w:sectPr")):
        for tag in ("w:headerReference", "w:footerReference", "w:pgNumType", "w:titlePg"):
            for node in sect.findall(qn(tag)):
                sect.remove(node)
    return False


def _insert_page_break(element: Any) -> None:
    """在段落末尾补一个分页符（等同于原件的"分节符（下一页）"的换页效果）。"""
    try:
        for run in element.findall(qn("w:r")):
            for br in run.findall(qn("w:br")):
                if br.get(qn("w:type")) == "page":
                    return          # 已经有分页符，不重复加
        run = OxmlElement("w:r")
        br = OxmlElement("w:br")
        br.set(qn("w:type"), "page")
        run.append(br)
        element.append(run)
    except Exception:  # noqa: BLE001
        pass


def _style_refs(elements: list[Any]) -> set[str]:
    """这些元素引用了哪些样式（段落/字符/表格）。"""
    ids: set[str] = set()
    for el in elements:
        for tag in ("w:pStyle", "w:rStyle", "w:tblStyle"):
            for node in el.iter(qn(tag)):
                value = node.get(qn("w:val"))
                if value:
                    ids.add(str(value))
    return ids


def _import_styles(document: Any, slice_doc: Any, elements: list[Any],
                   prefix: str = "ZBT") -> dict[str, str]:
    """把切片用到（及它们依赖）的样式搬进成稿，返回 `{原样式id: 成稿样式id}`。

    招标原件的段落几乎都是 `w:pStyle w:val="19"` 这种数字样式编号，缩进、行距、表格
    边框都写在样式里。不搬样式，成稿就只剩"内容对、位置全错"。
    样式 id 加前缀，避免和成稿自己的同名样式互相顶掉。
    """
    wanted = _style_refs(elements)
    if not wanted:
        return {}
    try:
        src_styles = slice_doc.styles.element
        target_styles = document.styles.element
    except Exception:  # noqa: BLE001
        return {}
    by_id: dict[str, Any] = {}
    for style in src_styles.findall(qn("w:style")):
        sid = style.get(qn("w:styleId"))
        if sid:
            by_id[str(sid)] = style
    mapping: dict[str, str] = {}
    queue = [sid for sid in wanted if sid in by_id]
    seen: set[str] = set()
    while queue:
        sid = queue.pop()
        if sid in seen:
            continue
        seen.add(sid)
        style = by_id.get(sid)
        if style is None:
            continue
        new_id = f"{prefix}{sid}"
        mapping[sid] = new_id
        cloned = copy.deepcopy(style)
        cloned.set(qn("w:styleId"), new_id)
        # 样式名也加前缀：同名样式在 Word 里会串味
        name = cloned.find(qn("w:name"))
        if name is not None:
            base_name = str(name.get(qn("w:val")) or sid)
            name.set(qn("w:val"), f"{prefix} {base_name}")
        for link_tag in ("w:basedOn", "w:next", "w:link"):
            node = cloned.find(qn(link_tag))
            if node is not None:
                ref = str(node.get(qn("w:val")) or "")
                if ref:
                    queue.append(ref)
        target_styles.append(cloned)
    # 依赖关系里的原 id 换成新 id
    for style in target_styles.findall(qn("w:style")):
        sid = str(style.get(qn("w:styleId")) or "")
        if not sid.startswith(prefix):
            continue
        for link_tag in ("w:basedOn", "w:next", "w:link"):
            node = style.find(qn(link_tag))
            if node is not None:
                ref = str(node.get(qn("w:val")) or "")
                if ref in mapping:
                    node.set(qn("w:val"), mapping[ref])
    return mapping


def _remap_style_refs(elements: list[Any], mapping: dict[str, str]) -> None:
    if not mapping:
        return
    for el in elements:
        for tag in ("w:pStyle", "w:rStyle", "w:tblStyle"):
            for node in el.iter(qn(tag)):
                value = str(node.get(qn("w:val")) or "")
                if value in mapping:
                    node.set(qn("w:val"), mapping[value])


# 切片里所有「指向本包另一个部件」的属性。`r:embed` 是 DrawingML 图片，
# `r:id` 是 VML `v:imagedata` 与页眉页脚引用，`r:link` 是外链，`r:pict` 是老式图片。
_REL_ATTRS = ("r:embed", "r:id", "r:link", "r:pict")


def _unique_partname(package: Any, src_partname: Any) -> Any:
    """给搬过来的部件挑一个在成稿包里不撞车的 partname。

    成稿自己也往 `word/media/` 放图（封面、资质页转图），直接从切片沿用
    `/word/media/image1.png` 会把成稿原有的同名部件顶掉，或者让 zip 里出现两条
    同路径条目——后者同样是「文件已损坏」的成因。
    """
    from docx.opc.packuri import PackURI

    base = str(src_partname)
    existing = {str(p.partname) for p in package.iter_parts()}
    if base not in existing:
        return src_partname
    stem, dot, ext = base.rpartition(".")
    index = 1
    while True:
        candidate = "%s_%d%s%s" % (stem, index, dot, ext)
        if candidate not in existing:
            return PackURI(candidate)
        index += 1


def _carry_rels(slice_doc: Any, document: Any, elements: list) -> int:
    """把 elements 引用到的关系（图片/超链接…）搬进成稿，并把这些引用改成新 rId。

    这是「按模板落章」最容易漏的一步：`copy.deepcopy` 只复制 XML 节点，节点上的
    `r:embed="rId28"` 指向的是**切片那个包**里的部件。切片包里的 `word/media/*`
    不会跟着节点走，成稿的 `document.xml.rels` 里也就没有 `rId28`。

    后果不是「图丢了」这么轻——Word 16 实测：只要有一个 VML `v:imagedata` 的
    `r:id` 在本 part 的 `.rels` 里解析不到，整份文件就被判「文件可能已经损坏」
    并拒绝打开（DrawingML `a:blip` 悬空反而只是渲染成空占位）。所以这里必须把
    部件本体也搬过去。

    返回被重写的引用数（0 表示切片本来就没有外部部件引用）。
    """
    from docx.opc.part import Part

    try:
        src_rels = slice_doc.part.rels
        target = document.part
    except Exception:  # noqa: BLE001 —— 搬不了关系时交给 _drop_dangling_rels 兜底
        return 0
    used: set[str] = set()
    for el in elements:
        for node in el.iter():
            if not isinstance(node.tag, str):
                continue
            for attr in _REL_ATTRS:
                value = node.get(qn(attr))
                if value:
                    used.add(str(value))
    mapping: dict[str, str] = {}
    for r_id in used:
        rel = src_rels.get(r_id)
        if rel is None:
            continue
        try:
            if getattr(rel, "is_external", False):
                mapping[r_id] = target.relate_to(
                    rel.target_ref, rel.reltype, is_external=True)
            else:
                src_part = rel.target_part
                # 部件本体挂到成稿的 package 上；partname 撞车就改名。
                # `[Content_Types].xml` 的 Default Extension 由 python-docx 保存时
                # 按各部件的 partname 后缀与 content_type 自动补齐（它按包内实际
                # part 反推，不需要手工写 <Default Extension="png">），补完仍可用
                # backend/tools/docx_validate.py 的 contentTypes 项复核。
                new_part = Part(_unique_partname(target.package, src_part.partname),
                                src_part.content_type, src_part.blob, target.package)
                mapping[r_id] = target.relate_to(new_part, rel.reltype)
        except Exception:  # noqa: BLE001 —— 单个部件搬不动就跳过，别拖垮整章
            continue
    if not mapping:
        return 0
    rewritten = 0
    for el in elements:
        for node in el.iter():
            if not isinstance(node.tag, str):
                continue
            for attr in _REL_ATTRS:
                key = qn(attr)
                value = node.get(key)
                if value in mapping:
                    node.set(key, mapping[value])
                    rewritten += 1
    return rewritten


def _drop_dangling_rels(document: Any, elements: list) -> int:
    """兜底：成稿包里解析不到的 `r:embed` / `r:id` / `r:link` 一律摘掉。

    这是「保证出稿一定能被 Word 打开」的最后一道闸。VML `v:imagedata` 的 `r:id`
    悬空是硬伤（Word 直接判文件损坏、拒绝打开），所以宁可少一张图，也不能让整本
    打不开。正常路径下 `_carry_rels` 已经把引用都接上了，这里应该摘不到东西；
    摘到了说明切片本身有问题（或被 `_prune_parts` 提前摘掉了图片部件）。
    """
    try:
        rels = document.part.rels
    except Exception:  # noqa: BLE001
        return 0
    dropped = 0
    for el in elements:
        for node in el.iter():
            if not isinstance(node.tag, str):
                continue
            for attr in _REL_ATTRS:
                key = qn(attr)
                value = node.get(key)
                if value and value not in rels:
                    del node.attrib[key]
                    dropped += 1
    return dropped


def append_to_document(document: Any, section_docx: bytes, *, keep_sections: bool = False) -> list:
    """把切片的 body 元素按顺序搬进目标文档，返回追加的元素列表。"""
    slice_doc = docx.Document(io.BytesIO(section_docx))
    # 库里已有的老切片是本次修复之前切的，段落里还留着 `w:sym`（Word 能显示，但平台的
    # 取文字逻辑读不到，字数/原文对照都缺字）。出稿前统一还原一次：不用等用户重传招标
    # 文件，老切片也能落出完整文字。
    normalize_symbols(slice_doc.element.body)
    body = document.element.body
    anchor = body.find(qn("w:sectPr"))
    source = [el for el in slice_doc.element.body if isinstance(el.tag, str) and el.tag != qn("w:sectPr")]
    mapping = _import_styles(document, slice_doc, source)
    appended: list[Any] = []
    for el in list(slice_doc.element.body):
        if not isinstance(el.tag, str):
            continue
        if el.tag == qn("w:sectPr"):
            if keep_sections:
                new_el = copy.deepcopy(el)
            else:
                continue
        else:
            new_el = copy.deepcopy(el)
            if not keep_sections:
                _strip_sectpr_keep_break(new_el)
        if anchor is not None:
            anchor.addprevious(new_el)
        else:
            body.append(new_el)
        appended.append(new_el)
    _remap_style_refs(appended, mapping)
    # 切片里的 `r:embed` / `r:id` 指向的是**切片那个包**里的部件（公章、证书扫描页…）。
    # 只 deepcopy XML 的话这些部件不跟着走，成稿就带着一堆解析不到的 rId 出门，
    # Word 会判「文件可能已经损坏」（实测只要一个 VML `v:imagedata` 悬空就够）。
    _carry_rels(slice_doc, document, appended)
    # 保险丝：还有解析不到的引用（页眉页脚、超链接、keep_sections 的分节符…）就摘掉属性。
    # 宁可少一张图，也不能让整本打不开。
    _drop_dangling_rels(document, appended)
    return appended


RED = "FF0000"

# 占位词：只有带这些词的括注才算"要客户填的地方"，避免把「（适用于无委托代理人的情况）」
# 这种说明性括注也标红。宁可少标一个说明，也不要把整篇标成红色。
_FILL_WORDS = (
    "名称|姓名|地址|电话|传真|邮编|编码|开户|账号|帐号|银行|金额|大写|小写|日期|"
    "签字|签章|盖章|公章|职务|身份证|注册|资质|编号|投标人|法定代表人|负责人|联系人|"
    "邮箱|网址|税号|信用代码|工期|质保|比例|单位|签收|法定代表"
)
# 括注里出现这些词的，是在解释规则，不是让人填
_NOT_FILL = re.compile(r"适用|详见|见第|如下|参见|例如|如需|如有")
# 「法定代表人（单位负责人）」是同义说明，不是空位
_SYNONYM_ONLY = ("单位负责人", "法定代表人", "委托代理人", "负责人")

_RED_PATTERNS = (
    re.compile(r"[（(][^（）()\n]{0,14}(?:%s)[^（）()\n]{0,14}[）)]" % _FILL_WORDS),
    re.compile(r"【[^【】\n]{0,14}(?:%s)[^【】\n]{0,14}】" % _FILL_WORDS),
    re.compile(r"年\s*月\s*日"),
    re.compile(r"[_＿]{3,}"),
    re.compile(r"此处[^\n，。；]{0,12}(?:公章|盖章|签字|签章)"),
)


def mark_customer_fields(elements: list) -> int:
    """把"要客户自己填"的地方标成红字，返回标了多少处。

    平台不替客户编企业信息（名称、地址、开户行、账号、法定代表人、签字盖章、日期、
    报价金额……）。这些空位原件里本来就有占位文字，一律留着，只把颜色改成红色，
    客户一眼就能找到该填哪里——**不改一个字**，版式与原件仍然逐项一致。
    """
    marked = 0
    for element in elements or ():
        for run in list(element.iter(qn("w:r"))):
            try:
                marked += _mark_run(run)
            except Exception:  # noqa: BLE001 —— 标红失败不影响出稿
                continue
    return marked


def _is_placeholder(text: str) -> bool:
    if not text or _NOT_FILL.search(text):
        return False
    return any(pattern.search(text) for pattern in _RED_PATTERNS)


def _red_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in _RED_PATTERNS:
        for match in pattern.finditer(text):
            piece = match.group(0)
            if piece[:1] in "（(【" and piece[-1:] in "）)】":
                inner = piece[1:-1].strip()
                # 「法定代表人（单位负责人）」这类同义说明不标
                if inner in _SYNONYM_ONLY:
                    continue
            spans.append((match.start(), match.end()))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clone_run(run: Any, text: str, *, red: bool):
    new = run.makeelement(qn("w:r"), {})
    rpr = run.find(qn("w:rPr"))
    new_rpr = copy.deepcopy(rpr) if rpr is not None else OxmlElement("w:rPr")
    if red:
        for old in new_rpr.findall(qn("w:color")):
            new_rpr.remove(old)
        color = OxmlElement("w:color")
        color.set(qn("w:val"), RED)
        new_rpr.append(color)
    new.append(new_rpr)
    node = OxmlElement("w:t")
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    node.text = text
    new.append(node)
    return new


def _mark_run(run: Any) -> int:
    """把一个 run 按占位符切开，占位部分单独成 run 并标红；返回标红段数。"""
    # 只处理"纯文字 run"：带图片、换行、域代码的 run 一律不碰，避免破坏原件结构
    if any(child.tag not in (qn("w:rPr"), qn("w:t")) for child in run):
        return 0
    texts = run.findall(qn("w:t"))
    if not texts:
        return 0
    full = "".join(node.text or "" for node in texts)
    spans = _red_spans(full)
    if not spans:
        return 0
    parent = run.getparent()
    if parent is None:
        return 0
    index = list(parent).index(run)
    cursor = 0
    marked = 0
    for start, end in spans:
        if start > cursor:
            parent.insert(index, _clone_run(run, full[cursor:start], red=False))
            index += 1
        parent.insert(index, _clone_run(run, full[start:end], red=True))
        index += 1
        marked += 1
        cursor = end
    if cursor < len(full):
        parent.insert(index, _clone_run(run, full[cursor:], red=False))
        index += 1
    parent.remove(run)
    return marked


def fill_section(section_docx: bytes, values: dict[str, str]) -> tuple[bytes, dict]:
    """把 values 里的值填进空位；没给值的空位原样保留。"""
    document = docx.Document(io.BytesIO(section_docx))
    report = {"filled": 0, "unfilled": 0, "skipped": []}
    blanks = {b["id"]: b for b in _blanks(document)}
    filled_ids: set[str] = set()
    for blank_id, value in (values or {}).items():
        blank = blanks.get(blank_id)
        text = str(value or "")
        if blank is None or not text.strip():
            report["skipped"].append(blank_id)
            continue
        if _fill_one(document, blank, text):
            report["filled"] += 1
            filled_ids.add(blank_id)
        else:
            report["skipped"].append(blank_id)
    report["unfilled"] = len([b for b in blanks.values() if b["id"] not in filled_ids])
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue(), report


def _fill_one(document: Any, blank: dict[str, Any], text: str) -> bool:
    if blank["kind"] == "cell":
        m = re.match(r"table(\d+):r(\d+):c(\d+)$", blank["id"])
        if not m:
            return False
        t_idx, r_idx, c_idx = (int(m.group(i)) for i in (1, 2, 3))
        try:
            cell = document.tables[t_idx].rows[r_idx].cells[c_idx]
        except (IndexError, KeyError):
            return False
        paragraph = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
        _insert_run(paragraph, text)
        return True
    if blank["kind"] == "underline":
        m = re.match(r"p(\d+):u(\d+)$", blank["id"])
        if not m:
            return False
        try:
            paragraph = document.paragraphs[int(m.group(1))]
        except IndexError:
            return False
        target = paragraph.runs[0] if paragraph.runs else None
        _insert_run(paragraph, text, before=target)
        return True
    if blank["kind"] == "bracket":
        m = re.match(r"p(\d+):b(\d+)$", blank["id"])
        if not m:
            return False
        try:
            paragraph = document.paragraphs[int(m.group(1))]
        except IndexError:
            return False
        match = _EMPTY_PAREN.search(paragraph.text)
        if match is None:
            return False
        for run in paragraph.runs:
            if not run.text:
                continue
            new_text = _EMPTY_PAREN.sub(f"（{text}）", run.text, count=1)
            if new_text != run.text:
                run.text = new_text
                return True
        _insert_run(paragraph, text)
        return True
    return False


def _insert_run(paragraph: Any, text: str, *, before: Any = None) -> Any:
    """插入一个 run，rPr 尽量沿用同一段落里已有的 run（字体字号才不会跳）。"""
    template_run = before or (paragraph.runs[0] if paragraph.runs else None)
    new_run = paragraph.add_run(text)
    if template_run is not None:
        try:
            rpr = template_run._element.find(qn("w:rPr"))
            if rpr is not None:
                new_run._element.insert(0, copy.deepcopy(rpr))
        except Exception:  # noqa: BLE001
            pass
    else:
        # 段落里没有 run：至少把段落标记的 rPr 抄过来（Word 里"段落标记"决定空段字体）
        ppr = paragraph._p.find(qn("w:pPr"))
        mark = ppr.find(qn("w:rPr")) if ppr is not None else None
        if mark is not None:
            new_run._element.insert(0, copy.deepcopy(mark))
    if before is not None and before._element.getparent() is not None:
        before._element.addprevious(new_run._element)
    return new_run


# ───────────────────────── 验收比对 ─────────────────────────

def _break_positions(document: Any) -> list[int]:
    positions: list[int] = []
    for idx, el in enumerate(_body_children(document)):
        count = len(el.findall(".//" + qn("w:br") + f"[@{qn('w:type')}='page']"))
        count += len(el.findall(".//" + qn("w:pageBreakBefore")))
        positions.extend([idx] * count)
    return positions


def _table_shape(document: Any) -> list[tuple[int, int, int]]:
    shape = []
    for table in document.tables:
        rows = len(table.rows)
        cols = max((len(row.cells) for row in table.rows), default=0)
        merged = len(table._tbl.findall(".//" + qn("w:gridSpan"))) + \
            len(table._tbl.findall(".//" + qn("w:vMerge")))
        shape.append((rows, cols, merged))
    return shape


def diff_report(baseline: bytes, output: bytes) -> dict:
    """验收：分页符位置、分节数、表格结构是否一致，以及差异是否只是"多了文字"。"""
    base_doc = docx.Document(io.BytesIO(baseline))
    out_doc = docx.Document(io.BytesIO(output))
    base_breaks = _break_positions(base_doc)
    out_breaks = _break_positions(out_doc)
    base_sections = len(base_doc.element.body.findall(".//" + qn("w:sectPr")))
    out_sections = len(out_doc.element.body.findall(".//" + qn("w:sectPr")))
    base_nodes = [el.tag for el in base_doc.element.body.iter() if isinstance(el.tag, str)]
    out_nodes = [el.tag for el in out_doc.element.body.iter() if isinstance(el.tag, str)]
    from collections import Counter

    base_count, out_count = Counter(base_nodes), Counter(out_nodes)
    added = sum((out_count - base_count).values())
    removed = sum((base_count - out_count).values())
    changed = 0
    for tag in set(base_count) & set(out_count):
        changed += abs(base_count[tag] - out_count[tag])
    return {
        "page_breaks_baseline": len(base_breaks),
        "page_breaks_output": len(out_breaks),
        "page_break_positions_same": base_breaks == out_breaks,
        "sections_baseline": base_sections,
        "sections_output": out_sections,
        "tables_same": _table_shape(base_doc) == _table_shape(out_doc),
        "added_nodes": added,
        "removed_nodes": removed,
        "changed_nodes": changed,
        # 允许的差异只有「新增文字 run」与"给首段加的大纲级别"：两者都不产生 removed
        "only_text_added": removed == 0,
    }