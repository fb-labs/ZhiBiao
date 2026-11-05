"""导出时的标题归一：只处理独立标题行，不改写正文与表格。

来自智标 2.0（Zhibiao_lite）的导出结构规则：
- 去掉章节正文开头重复的章标题；
- 把 `#`/裸点号标题换算成连续层级；
- 章号以大纲为准（1、1.1、1.1.1），避免模型把全局序号写进标题。
"""
from __future__ import annotations

import re

_CN_NUM = "一二三四五六七八九十"
SUBHEADING_NUMBER_RE = re.compile(
    r"^[ \t\u3000]*(?:"
    r"[（(][ \t]*(?:\d+(?:[.．]\d+)*|[%s]+)[ \t]*[)）]"
    r"|第[ \t]*\d+[ \t]*[章节部分条款项]"
    r"|第[ \t]*[%s]+[章节部分条款项]"
    r"|第[ \t]*(?:\d+|[%s]+)(?=[ \t.．、:：)）]|$)"
    r"|\d+(?:[.．]\d+)+"
    r"|\d+[.．、)）:：]"
    r"|[%s]+[、.．)）:：]"
    r")" % ((_CN_NUM,) * 4)
)
_MARKDOWN = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_BARE = re.compile(r"^[ \t]{0,3}(\d+(?:[.．]\d+)+)[ \t]+(.+?)[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_SENTENCE = re.compile(r"[。！？；;!?，,：:]|[.]$")
_IDENTIFIER = re.compile(r"^(?:[A-Z]{1,8}[-_/]\d|\d|[Φφ]|(?:mm|MPa|kN|kg|℃|%)(?:\b|$))")


def _without_number(text: str) -> str:
    value = str(text or "").strip()
    match = SUBHEADING_NUMBER_RE.match(value)
    if match:
        value = value[match.end():].lstrip(" .．、)）:：\t")
    return value


def _title_key(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("**") and value.endswith("**"):
        value = value[2:-2].strip()
    return re.sub(r"\s+", "", _without_number(value)).casefold()


def _heading(line: str) -> tuple[int, str, str] | None:
    matched = _MARKDOWN.match(line)
    if matched:
        return len(matched.group(1)), matched.group(2), ""
    matched = _BARE.match(line)
    if not matched:
        return None
    label, text = matched.groups()
    if (
        not text
        or len(text) > 64
        or _SENTENCE.search(text)
        or _IDENTIFIER.match(text)
        or "|" in text
        or "{{" in text
        or not re.search(r"[A-Za-z\u3400-\u9fff]", text)
    ):
        return None
    return 0, text, label.replace("．", ".")


def normalize_export_structure(
    content: str,
    title: str,
    chapter_number: str = "",
    *,
    container: bool = False,
) -> str:
    lines = str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    key = _title_key(title)

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        matched = _MARKDOWN.match(line)
        candidate = matched.group(2) if matched else line.strip()
        if key and _title_key(candidate) == key:
            lines[index] = ""
            continue
        break

    headings: list[tuple[int, int, str, str]] = []
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
        found = _heading(line)
        if found:
            depth, text, label = found
            headings.append((index, depth, text, label))
    if not headings:
        return "\n".join(lines)

    markdown_min = min((h[1] for h in headings if h[1]), default=2)
    bare_min = min((len(h[3].split(".")) for h in headings if h[3]), default=1)
    number = str(chapter_number or "").strip()
    counters: list[int] = []
    depth_stack: list[int] = []
    for index, markdown_depth, text, old_number in headings:
        if markdown_depth:
            requested = markdown_depth - markdown_min + 1
            clean = _without_number(text) if number else text
        else:
            clean = text if number else (old_number + " " + text)
            requested = (
                len(old_number.split(".")) - len(number.split("."))
                if number and old_number.startswith(number + ".")
                else len(old_number.split(".")) - bare_min + 1
            )
        while depth_stack and depth_stack[-1] >= requested:
            depth_stack.pop()
        depth_stack.append(requested)
        depth = min(len(depth_stack), 5)
        if len(counters) < depth:
            counters.append(1)
        else:
            del counters[depth:]
            counters[-1] += 1
        prefix = ""
        if number and not container:
            prefix = ".".join([number, *(str(value) for value in counters)]) + " "
        lines[index] = "#" * (depth + 1) + " " + prefix + clean
    return "\n".join(lines)
