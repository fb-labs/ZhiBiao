"""把外部人工编写的标书正文切成可检查的章节结构（投标检查「导入标书」用）。

投标检查原先只能读平台自己生成的 `project_chapters`。用户拿自己写好的标书来查时，
必须在**不覆盖任何生成结果**的前提下，把它变成同一套检查入口能消费的章节列表。

设计约束：
- 只做结构性切分，不改写、不摘要、不纠错正文；正文逐字保留（含页码、页眉页脚残留）。
- 每条记录保留 1 基行号区间，判定结果才能回溯到用户文档的具体位置。
- 认不出版式就退化为单章，并把原因写进 note —— 猜错层级会让「目录与章节编号规范」
  这类规则报出假问题，比不切分更有害。
- 不丢正文：标题数超过上限时把余下内容并入最后一条，宁可章节粗，也不静默截断。
- 纯函数：无 I/O、无模型调用、不读数据库。
"""
from __future__ import annotations

import re
from typing import Any

# 单份导入标书最多切成多少条。超过时余下内容并入最后一条（不丢字）。
MAX_CHAPTERS = 240
MAX_LEVEL = 6
# 标题行长度上限：超过这个长度的「第X章 …」几乎都是正文里在转述条款。
_HEADING_MAX_LEN = 64
# 前言片段至少要有这么多字才单独成条，否则并入后面。
_PREAMBLE_MIN_CHARS = 20

_NUM_TOKEN = r"(?:\d{1,3}|[一二三四五六七八九十百零两]{1,4})"
_MD = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_TABLE_ROW = re.compile(r"^[ \t]*\|")
_CAPTION = re.compile(r"^[ \t]*(?:图|表|附图|附表|Fig|Table)\s*[.．]?\s*\d")
_CHAPTER = re.compile(rf"^第\s*{_NUM_TOKEN}\s*(?:章|篇|部分)[ \t\u3000、.．:：]*(.*)$")
_SECTION = re.compile(rf"^第\s*{_NUM_TOKEN}\s*节[ \t\u3000、.．:：]*(.*)$")
_CLAUSE = re.compile(rf"^第\s*{_NUM_TOKEN}\s*(?:条|款|项)[ \t\u3000、.．:：]*(.*)$")
_PAREN = re.compile(
    r"^[（(][ \t]*(?:\d+(?:[.．]\d+)*|[一二三四五六七八九十百零两]{1,4})[ \t]*[)）]"
    r"[ \t]*[、.．:：]?[ \t]*(.*)$")
_CN_ITEM = re.compile(r"^(?:[一二三四五六七八九十百零两]{1,4})[ \t]*[、.．][ \t]*(.*)$")
_DOTTED_SPACE = re.compile(r"^(\d{1,2}(?:[.．]\d{1,2}){0,4})[ \t]+(\S.*)$")
_DOTTED_SEP = re.compile(r"^(\d{1,2}(?:[.．]\d{1,2}){0,4})[、:：)）][ \t]*(\S.*)$")
# 以数值+单位开头的行（`1.2 MPa`、`3 种工况` 之类）不是标题
_VALUE_START = re.compile(
    r"^(?:[A-Za-z]{1,8}[-_/]\d|\d|Φ|φ|mm|MM|MPa|kN|KN|kg|KG|℃|%|±|～|~)")
_SENTENCE_TAIL = re.compile(r"[。！？；;]$")

# 家族 → 相对层级提示（归一前的 rank）
_RANK = {"chapter": 1, "section": 2, "clause": 3, "paren": 3, "cn": 2, "md": 1}


def _words(text: str) -> int:
    """与项目章节一致的字数口径：去空白后的字符数（见 pipeline_api 保存正文处）。"""
    return len(re.sub(r"\s", "", str(text or "")))


def _clean_title(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().strip("*_# \t")).strip()


def _is_title(text: str) -> bool:
    """标题行的保守判据：不满足就按正文处理（宁可漏切，不可错切）。"""
    value = _clean_title(text)
    if not value or len(value) > _HEADING_MAX_LEN:
        return False
    if _SENTENCE_TAIL.search(value) or "。" in value or "；" in value:
        return False
    if "|" in value or "{{" in value or "：" in value[-1:]:
        return False
    if _VALUE_START.match(value):
        return False
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", value))


def _classify(line: str) -> tuple[str, int, str] | None:
    """返回 (家族, 原始 rank, 标题)；不是标题返回 None。"""
    matched = _MD.match(line)
    if matched:
        text = matched.group(2)
        return ("md", len(matched.group(1)), _clean_title(text)) if _is_title(text) else None
    stripped = line.strip()
    if not stripped or _TABLE_ROW.match(stripped) or _CAPTION.match(stripped):
        return None
    for family, pattern, rank in (
        ("chapter", _CHAPTER, 1), ("section", _SECTION, 2), ("clause", _CLAUSE, 3),
        ("paren", _PAREN, 3), ("cn", _CN_ITEM, 2),
    ):
        matched = pattern.match(stripped)
        if matched:
            text = matched.group(1)
            # 「第三章」这类没有标题的纯编号行也接受，标题留空即可
            if text.strip() and not _is_title(text):
                return None
            return (family, rank, _clean_title(text))
    for pattern in (_DOTTED_SPACE, _DOTTED_SEP):
        matched = pattern.match(stripped)
        if matched:
            label, text = matched.groups()
            if not _is_title(text):
                return None
            dots = label.replace("．", ".").count(".")
            return ("num", 1 + min(dots + 1, 4), _clean_title(text))
    return None


def split_text_into_chapters(text: str) -> dict[str, Any]:
    """把标书全文切成 [{seq, level, title, words, content, line_start, line_end}]。

    返回 dict：chapters / strategy / heading_count / truncated / note。
    `strategy` 为 "headings" 时按识别到的标题切分，为 "single" 时整篇算一章。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.split("\n")
    total_chars = _words(raw)

    candidates: list[dict[str, Any]] = []
    fence = ""
    for index, line in enumerate(lines):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        found = _classify(line)
        if found:
            family, rank, title = found
            candidates.append({"index": index, "family": family, "rank": rank, "title": title})

    if not candidates:
        # 一个标题都没认出来：整篇当一章。检查仍然能跑，只是依赖层级的规则结论不具参考性。
        title = _clean_title(lines[0]) if lines and _is_title(lines[0]) else "（全文）"
        note = ("未识别到章节标题，已按单章整体检查；"
                "「目录与章节编号规范」等依赖层级的结论仅供参考。")
        return {
            "chapters": [{"seq": 1, "level": 1, "title": title[:80] or "（全文）",
                          "words": _words(raw), "content": raw,
                          "line_start": 1, "line_end": max(1, len(lines))}],
            "strategy": "single", "heading_count": 0, "truncated": False, "note": note,
        }

    # 有的文档「一、」就是顶层，有的挂在「第X章」下：按是否出现过章标题决定。
    has_chapter = any(c["family"] == "chapter" for c in candidates)
    for item in candidates:
        if item["family"] == "cn":
            item["rank"] = 2 if has_chapter else 1
        elif item["family"] == "md":
            item["rank"] = min(item["rank"], MAX_LEVEL)

    # 归一：观察到的最浅层级映射为 1，并把跳级压平（10.2.1.1 这种不再产生断层）。
    lowest = min(c["rank"] for c in candidates)
    previous = 1
    for item in candidates:
        requested = item["rank"] - lowest + 1
        level = max(1, min(requested, previous + 1, MAX_LEVEL))
        item["level"] = level
        previous = level

    truncated = len(candidates) > MAX_CHAPTERS
    kept = candidates[:MAX_CHAPTERS]

    chapters: list[dict[str, Any]] = []
    # 首个标题之前的内容单独成条，避免前言/封面信息被丢掉
    first_index = kept[0]["index"]
    preamble = "\n".join(lines[:first_index]).strip()
    if _words(preamble) >= _PREAMBLE_MIN_CHARS:
        chapters.append({"seq": 1, "level": 1, "title": "（未编号内容）",
                         "words": _words(preamble), "content": preamble,
                         "line_start": 1, "line_end": max(1, first_index)})

    for position, item in enumerate(kept):
        start = item["index"]
        end = kept[position + 1]["index"] if position + 1 < len(kept) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        chapters.append({
            "seq": len(chapters) + 1, "level": item["level"],
            "title": item["title"] or ("第 %d 节" % (len(chapters) + 1)),
            "words": _words(body), "content": body,
            "line_start": start + 1, "line_end": max(start + 1, end),
        })

    if truncated:
        # 余下标题不再单独成条，但它们的正文必须保留：并入最后一条。
        tail = "\n".join(lines[kept[-1]["index"]:]).strip()
        chapters[-1]["content"] = tail
        chapters[-1]["words"] = _words(tail)
        chapters[-1]["line_end"] = max(1, len(lines))

    note = "已按识别到的 %d 个标题切分为 %d 条，用于逐条比对招标要求。" % (
        len(candidates), len(chapters))
    if truncated:
        note += ("标题数超过 %d 条上限，余下内容已合并到最后一条，正文未丢失，"
                 "但章节颗粒度变粗。" % MAX_CHAPTERS)
    return {
        "chapters": chapters, "strategy": "headings", "heading_count": len(candidates),
        "truncated": truncated, "note": note,
    }