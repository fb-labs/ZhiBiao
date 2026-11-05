# -*- coding: utf-8 -*-
"""按 PDF 招标文件的版式取页、转图、验收。

**许可前提**：本模块只用仓库允许的库（见 `requirements.txt` 的许可红线）——
`pypdf`（BSD-3）负责无损拷贝页，`pdfplumber`（MIT，底层 pypdfium2=BSD/Apache）负责
文字层几何与页面渲染。**不用 PyMuPDF**（AGPL，仓库明令禁止）。

和 Word 那条路的区别：PDF 没有"段落/表格"这层结构，任何"重排"都会把字距、行距、
页码、图章一起弄坏。所以这里只做三种**不改动既有内容**的动作：

  1. 取页（`extract_section_pdf`）：把格式章的小节所在页整页复制出来。pypdf 复制页是
     原样搬运内容流——实测未改动页的内容流与原件**逐字节相同**，不重新排版。
  2. 转图（`section_page_images`）：逐页渲染成 PNG 供插入 Word 成稿。
  3. 验收（`verify_fidelity`）：逐页比对，确认页数、内容流、文字一个都没少。

"毫米级对齐"这种话不讲：真正能保证的是**未改动的页与原件的渲染结果完全一致**。
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any

import pdfplumber
import pypdf

# 章标题必须提到这些词之一；小节标题形如「一、投标函」「（二）身份证明」
_TEMPLATE_TITLE = re.compile(
    r"((?:投标|响应|报价)文件(?:的)?(?:有关|相关|参考|主要|必备)?(?:格式|组成|组成部分))")
_CHAPTER_HEAD = re.compile(r"^\s*第[一二三四五六七八九十百]+[章节部分]")
_SECTION_HEAD = re.compile(r"^\s*(?:[一二三四五六七八九十]{1,3}\s*[、.．)]|[（(][一二三四五六七八九十]{1,3}[）)])")
_UNDERLINE = re.compile(r"[_＿]{3,}")
_NUM_PREFIX = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+[章节部分]|[一二三四五六七八九十百]{1,3}\s*[、.．)]|"
    r"[（(][一二三四五六七八九十百\d]{1,3}[）)])\s*")
_SPACE = re.compile(r"[\s\u3000]+")
_PUNCT = re.compile(r"[。，、；：.,;:（）()【】\[\]“”\"'’‘]")

_MIN_HEADING_SIZE = 8.0
_MAX_HEADING_CHARS = 40

_CACHE: dict[str, Any] = {}
_CACHE_LIMIT = 4


def normalize_title(text: str) -> str:
    return _SPACE.sub("", _PUNCT.sub("", _NUM_PREFIX.sub("", str(text or "").strip())))


def _key(raw: bytes) -> str:
    return hashlib.sha1(bytes(raw)[:400_000] + str(len(raw)).encode()).hexdigest()[:16]


def _open(raw: bytes) -> Any:
    """pdfplumber 文档对象（只读，按内容键缓存：一次导出要读十几页）。"""
    key = _key(raw)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    doc = pdfplumber.open(io.BytesIO(bytes(raw)))
    if len(_CACHE) >= _CACHE_LIMIT:
        old = _CACHE.pop(next(iter(_CACHE)), None)
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass
    _CACHE[key] = doc
    return doc


def _lines(page: Any) -> list[dict[str, Any]]:
    """按阅读顺序取每行的文字、字号、包围盒。

    pdfplumber 的 `extract_text_lines` 会按基线把字符归行，字符上带 size/fontname，
    这正是判断"这行是不是标题"需要的全部信息。
    """
    out: list[dict[str, Any]] = []
    try:
        rows = page.extract_text_lines(return_chars=True)
    except Exception:  # noqa: BLE001 —— pdfplumber 版本差异或坏页：当作没有文字
        return out
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        chars = row.get("chars") or []
        size = max((float(c.get("size") or 0) for c in chars), default=0.0)
        font = str(chars[0].get("fontname") or "") if chars else ""
        out.append({"text": text, "size": round(size, 1), "font": font,
                    "bbox": (float(row.get("x0") or 0), float(row.get("top") or 0),
                             float(row.get("x1") or 0), float(row.get("bottom") or 0))})
    return out


def _pages(raw: bytes) -> list[list[dict[str, Any]]]:
    """整份 PDF 的逐页行（只取文字层，纯计算，不渲染）。"""
    doc = _open(raw)
    pages: list[list[dict[str, Any]]] = []
    for page in doc.pages:
        pages.append(_lines(page))
    return pages


def _looks_like_section_title(text: str) -> bool:
    """小节标题是短名词短语：「一、投标函」「（二）身份证明」。

    正文里也有以「一、」「三、」开头的清单项（例如承诺书条款"一、我单位遵守国家招投标
    法律法规…"），它们带逗号句号、句子长，必须排除，否则会被当成小节去切页。
    """
    value = str(text or "").strip()
    if not value or len(value) > 30:
        return False
    return not re.search(r"[。；;，,：:]", value)


def _is_heading(line: dict[str, Any], pattern: re.Pattern[str]) -> bool:
    text = line["text"]
    if not text or len(text) > _MAX_HEADING_CHARS or line["size"] < _MIN_HEADING_SIZE:
        return False
    if not pattern.match(text):
        return False
    return not re.search(r"[。；;，,]$", text)


# ───────────────────────── 识别与切分（纯计算，可单测） ─────────────────────────

def _body_size(pages: list[list[dict[str, Any]]]) -> float:
    """正文字号：全文出现次数最多的字号（页眉页脚的字号通常不占多数）。"""
    counts: dict[float, int] = {}
    for lines in pages:
        for line in lines:
            if line["size"] < _MIN_HEADING_SIZE:
                continue
            counts[line["size"]] = counts.get(line["size"], 0) + 1
    if not counts:
        return 0.0
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _page_max_size(lines: list[dict[str, Any]]) -> float:
    return max((line["size"] for line in lines), default=0.0)


def _is_chapter_title(line: dict[str, Any], body_size: float) -> bool:
    """这一行是不是「第X章 …」这种真章标题（用来给格式章划终点）。

    正文换行到行首的条款常常以「第二章"投标人须知"第 1.4.3 项…」开头，
    它同样匹配 `^第…章`，但：字数很长、含「第1.4.3项」这类续写标记、字号与正文一样。
    三条里中任意一条就判定为「不是章标题」。
    """
    text = str(line.get("text") or "").strip()
    if not _CHAPTER_HEAD.match(text):
        return False
    if len(text) > 30:
        return False
    if re.search(r"第\s*\d+(?:\.\d+)*\s*[项条款目章节]|[条款项目]$", text):
        return False
    size = float(line.get("size") or 0.0)
    if size and body_size and size < body_size * 1.15:
        return False
    return True


def _section_level(text: str) -> int:
    value = str(text or "").strip()
    if re.match(r"^[一二三四五六七八九十]{1,3}\s*[、.．)]", value):
        return 1
    if re.match(r"^[（(][一二三四五六七八九十]{1,3}[）)]", value):
        return 2
    return 0


def sections_from_pages(pages: list[list[dict[str, Any]]]) -> tuple[str, int, list[dict[str, Any]]]:
    """从逐页行数据里找出格式章与小节，返回 (章标题, 章起始页下标, 小节列表)。

    纯函数：不碰 PDF 库，便于单测（真实样本的坑都在这里，而不是在拷贝页那一步）。
    """
    body_size = _body_size(pages)
    candidates: list[tuple[int, str]] = []
    for page_no, lines in enumerate(pages):
        for line in lines:
            text = line["text"]
            if len(text) > 40 or not _TEMPLATE_TITLE.search(text):
                continue
            # 「电子投标文件组成要求：本次招标以…」这类正文句子不是章标题
            if re.search(r"[：:；;，,。]$", text) or re.search(r"[：:]", text):
                continue
            if not (_CHAPTER_HEAD.match(text) or len(text) <= 30):
                continue
            candidates.append((page_no, text))
    best: tuple[str, int, list[dict[str, Any]]] = ("", -1, [])
    for page_no, text in candidates:
        found = _collect(pages, page_no, body_size)
        if len(found) >= 2 and len(found) >= len(best[2]):
            best = (text, page_no, found)
    return best


def _collect(pages: list[list[dict[str, Any]]], start: int, body_size: float) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    toc_pages: set[int] = set()
    for page_no in range(start, len(pages)):
        lines = pages[page_no]
        page_hits: list[dict[str, Any]] = []
        chapter_break = False
        page_max = _page_max_size(lines)
        floor_page = page_max * 0.9
        floor_body = body_size * 1.15
        # 只有两三页的文件里"正文字号"可能就等于标题字号，这时不能再按"比正文大 15%"压
        heading_floor = floor_page if (page_max and body_size >= floor_page) else max(floor_body, floor_page)
        for line in lines:
            text = line["text"]
            if (page_no > start and len(text) <= 60
                    and not _TEMPLATE_TITLE.search(text)
                    and _is_chapter_title(line, body_size)):
                chapter_break = True
                break
            if not (_is_heading(line, _SECTION_HEAD) and _looks_like_section_title(text)):
                continue
            # 「一、」级标题：正文里的条款与正文同字号，真标题明显更大
            if _section_level(text) == 1 and heading_floor and line["size"] < heading_floor:
                continue
            page_hits.append({"title": text, "page": page_no, "bbox": line["bbox"]})
        # 一页上密排 6 个以上标题 = 这一页是小节目录，不是正文
        if len(page_hits) >= 6:
            toc_pages.add(page_no)
        hits.extend(page_hits)
        if chapter_break:
            break
    hits = [hit for hit in hits if hit["page"] not in toc_pages]
    # 章首目录里常写「二、授权委托书（适用于有委托代理人的情况）」，正文标题更短；
    # 同页上更长的那个是目录条目，去掉。
    kept: list[dict[str, Any]] = []
    for hit in hits:
        key = normalize_title(hit["title"])
        longer_variant = any(
            other is not hit and other["page"] == hit["page"]
            and normalize_title(other["title"]) != key
            and key.startswith(normalize_title(other["title"]))
            for other in hits)
        if longer_variant:
            continue
        kept.append(hit)
    dedup: dict[str, dict[str, Any]] = {}
    for hit in kept:
        dedup[hit["title"].strip()] = hit
    ordered = sorted(dedup.values(), key=lambda h: (h["page"], h["bbox"][1]))
    for pos, hit in enumerate(ordered):
        hit["index"] = pos
        hit["match_key"] = normalize_title(hit["title"])
    return ordered


# ───────────────────────── 对外 API ─────────────────────────

def page_count(raw: bytes) -> int:
    """只数页数（不解析内容，毫秒级），用于先判断值不值得做识别。"""
    try:
        return len(pypdf.PdfReader(io.BytesIO(bytes(raw))).pages)
    except Exception:  # noqa: BLE001
        return 0


def detect(raw: bytes, filename: str = "") -> dict:
    """有没有「投标文件格式」章、在哪几页、有哪些小节。"""
    try:
        pages = _pages(raw)
    except Exception as exc:  # noqa: BLE001
        return {"has_template": False, "kind": "", "scope_title": "", "section_titles": [],
                "start_page": -1, "end_page": -1,
                "reason": f"不是可解析的 PDF：{exc.__class__.__name__}"}
    if not pages:
        return {"has_template": False, "kind": "", "scope_title": "", "section_titles": [],
                "start_page": -1, "end_page": -1, "reason": "PDF 没有页面"}
    scope, start, sections = sections_from_pages(pages)
    if start < 0:
        return {"has_template": False, "kind": "", "scope_title": "", "section_titles": [],
                "start_page": -1, "end_page": -1, "reason": "没有找到标题含「投标文件格式」的章"}
    if len(sections) < 2:
        return {"has_template": False, "kind": "", "scope_title": scope, "section_titles": [],
                "start_page": start + 1, "end_page": -1,
                "reason": "格式章里不足 2 个可切分的小节"}
    return {"has_template": True, "kind": "pdf", "scope_title": scope,
            "section_titles": [s["title"] for s in sections],
            "start_page": start + 1, "end_page": len(pages),
            "pages": len(pages), "reason": ""}


def _scope_end_page(pages: list[list[dict[str, Any]]], start: int, body_size: float) -> int:
    """格式章的最后一页：其后第一个「真章标题」所在页的前一页；后面没有章就到最后。

    不做这个上限，最后一个小节会把整本剩下所有页（别的章节、附件）都吞进来。
    """
    for page_no in range(start + 1, len(pages)):
        for line in pages[page_no]:
            if (_is_chapter_title(line, body_size)
                    and not _TEMPLATE_TITLE.search(str(line.get("text") or ""))):
                return max(start, page_no - 1)
    return len(pages) - 1


def extract_sections(raw: bytes, filename: str = "") -> list[dict]:
    """小节清单：标题、归一化键、起止页（1 基）、页内下划线空位数。"""
    try:
        pages = _pages(raw)
    except Exception:  # noqa: BLE001
        return []
    scope, start, sections = sections_from_pages(pages)
    if start < 0:
        return []
    body_size = _body_size(pages)
    scope_end = _scope_end_page(pages, start, body_size)
    out: list[dict] = []
    for pos, sec in enumerate(sections):
        first = sec["page"]
        # 小节止于「下一节标题页的前一页」——早期写成 next["page"]，
        # 结果 24 节里有 22 节的标题页被算进上一节，成稿里就出现整节渲染 0 页。
        last = (sections[pos + 1]["page"] - 1) if pos + 1 < len(sections) else scope_end
        last = max(first, min(last, scope_end))
        blanks = _page_blanks(pages, first, last)
        out.append({"title": sec["title"], "match_key": sec["match_key"], "scope": scope,
                    "index": pos, "start_page": first + 1, "end_page": last + 1,
                    "page_breaks": max(0, last - first), "blanks": blanks})
    return out


def _page_blanks(pages: list[list[dict[str, Any]]], first: int, last: int) -> list[dict[str, Any]]:
    """下划线空位（正文里 `______` 那种）。表格空格子在 PDF 文字层里没有可依的标记，不猜。"""
    blanks: list[dict[str, Any]] = []
    for page_no in range(first, min(last, len(pages) - 1) + 1):
        for line in pages[page_no]:
            for _match in _UNDERLINE.finditer(line["text"]):
                blanks.append({"id": f"pdf_p{page_no}:u{len(blanks)}", "kind": "underline",
                               "page": page_no, "rect": list(line["bbox"]),
                               "label": "", "expect": "text"})
    return blanks


def _copy_pages(raw: bytes, first: int, last: int) -> bytes:
    """无损拷贝页（内容流原样搬运，不重排、不栅格化）。"""
    reader = pypdf.PdfReader(io.BytesIO(bytes(raw)))
    writer = pypdf.PdfWriter()
    for index in range(first, min(last, len(reader.pages) - 1) + 1):
        writer.add_page(reader.pages[index])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def render_pages(raw: bytes, first: int, last: int, *, dpi: int = 150) -> list[dict[str, Any]]:
    """按**绝对页号**（0 基，含首含尾）逐页渲染 PNG，不重新识别章节。

    导出时用这个而不是 `section_page_images`：章节的页范围已经在上传时存进库了，
    导出不该为了取几页把 100 多页的文字层再扫一遍（实测那要 20 秒）。
    """
    if not raw or last < first:
        return []
    try:
        blob = _copy_pages(raw, first, last)
    except Exception:  # noqa: BLE001
        return []
    images: list[dict[str, Any]] = []
    try:
        doc = pdfplumber.open(io.BytesIO(blob))
    except Exception:  # noqa: BLE001
        return []
    try:
        resolution = max(72, int(dpi))
        for index, page in enumerate(doc.pages):
            try:
                image = page.to_image(resolution=resolution).original
            except Exception:  # noqa: BLE001 —— 渲染后端缺失就放弃转图
                return []
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            images.append({"page": first + index, "png": buf.getvalue(),
                           "width_pt": float(page.width), "height_pt": float(page.height)})
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return images


def extract_section_pdf(raw: bytes, title: str) -> bytes | None:
    """把一个小节所在的整页原样复制成新的 PDF。"""
    wanted = normalize_title(title)
    if not wanted or not raw:
        return None
    try:
        pages = _pages(raw)
    except Exception:  # noqa: BLE001
        return None
    _scope, start, sections = sections_from_pages(pages)
    if start < 0:
        return None
    for pos, sec in enumerate(sections):
        if sec["match_key"] != wanted and normalize_title(sec["title"]) != wanted:
            continue
        first = sec["page"]
        last = sections[pos + 1]["page"] if pos + 1 < len(sections) else len(pages) - 1
        try:
            return _copy_pages(raw, first, last)
        except Exception:  # noqa: BLE001
            return None
    return None


def section_page_images(raw: bytes, title: str, *, dpi: int = 150) -> list[dict[str, Any]]:
    """把一个小节的页逐页渲染成 PNG，供插入 Word 成稿。

    为什么是图而不是转 docx：PDF 的版式由内容流决定，转成 docx 必然重排（表格错位、
    页码丢失、印章消失）。转图能保证"评审看到的这一页 == 招标文件里那一页"，
    代价是那几页在 Word 里不可编辑——这是 PDF 标书的固有取舍。
    """
    blob = extract_section_pdf(raw, title)
    if not blob:
        return []
    images: list[dict[str, Any]] = []
    try:
        doc = pdfplumber.open(io.BytesIO(blob))
    except Exception:  # noqa: BLE001
        return []
    try:
        resolution = max(72, int(dpi))
        for index, page in enumerate(doc.pages):
            try:
                image = page.to_image(resolution=resolution).original
            except Exception:  # noqa: BLE001 —— 渲染后端缺失就放弃转图（调用方回落文本）
                return []
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            images.append({"page": index, "png": buf.getvalue(),
                           "width_pt": float(page.width), "height_pt": float(page.height)})
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return images


# ───────────────────────── 验收 ─────────────────────────

def _page_streams(raw: bytes) -> list[str]:
    """每页内容流的 sha1：未改动的页必须与原件逐字节相同。"""
    reader = pypdf.PdfReader(io.BytesIO(bytes(raw)))
    out: list[str] = []
    for page in reader.pages:
        try:
            data = page.get_contents().get_data()
        except Exception:  # noqa: BLE001
            data = b""
        out.append(hashlib.sha1(data).hexdigest())
    return out


def _page_texts(raw: bytes) -> list[str]:
    doc = _open(raw)
    try:
        return [(page.extract_text() or "") for page in doc.pages]
    except Exception:  # noqa: BLE001
        return []


def verify_fidelity(baseline: bytes, output: bytes, *, changed_pages: list[int] | None = None) -> dict:
    """逐页验收：页数不变；未改动的页内容流逐字节相同；原有文字一个字没少。"""
    report: dict[str, Any] = {"ok": True, "issues": []}
    try:
        base_streams = _page_streams(baseline)
        out_streams = _page_streams(output)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [f"PDF 无法打开：{exc.__class__.__name__}"]}
    report["pages_baseline"] = len(base_streams)
    report["pages_output"] = len(out_streams)
    if len(base_streams) != len(out_streams):
        report["ok"] = False
        report["issues"].append("页数变了")
        return report
    touched = set(changed_pages or [])
    stream_changed = [i + 1 for i, (a, b) in enumerate(zip(base_streams, out_streams))
                      if a != b and i not in touched]
    if stream_changed:
        report["ok"] = False
        report["issues"].append(f"第 {stream_changed} 页在未改动的情况下内容流变了")
    report["stream_changed_pages"] = stream_changed
    base_text = _page_texts(baseline)
    out_text = _page_texts(output)
    missing = [i + 1 for i, (a, b) in enumerate(zip(base_text, out_text)) if not _text_kept(a, b)]
    report["pages_missing_text"] = missing
    if missing:
        report["ok"] = False
        report["issues"].append(f"第 {missing} 页原有文字变少")
    report["text_identical_pages"] = sum(1 for a, b in zip(base_text, out_text) if a == b)
    return report


def _text_kept(before: str, after: str) -> bool:
    """原有文字必须全部还在（允许插入新字）。"""
    compact = lambda s: _SPACE.sub("", s)  # noqa: E731
    need, have = compact(before), compact(after)
    if need in have:
        return True
    for line in [compact(x) for x in before.splitlines() if x.strip()]:
        if line and line not in have:
            return False
    return True


def diff_report(baseline: bytes, output: bytes) -> dict:
    """取页前后的机械比对，供导出后自检用。"""
    base_text = _page_texts(baseline)
    out_text = _page_texts(output)
    return {
        "pages_baseline": len(base_text),
        "pages_output": len(out_text),
        "pages_text_identical": sum(1 for a, b in zip(base_text, out_text) if a == b),
        "pages_changed": [i + 1 for i, (a, b) in enumerate(zip(base_text, out_text)) if a != b],
        "only_text_added": _text_kept("".join(base_text), "".join(out_text)),
        "streams_same": _page_streams(baseline) == _page_streams(output),
    }