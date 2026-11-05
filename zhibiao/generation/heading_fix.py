"""正文内嵌小标题归一：删空标题、把编号重接成「本章号 + 本地层级」。"""

from __future__ import annotations

import re

from zhibiao.core.chapter_rules import (
    normalize_inline_numbering,
    strip_inline_numbering,
)

_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
_HEADING_LINE = re.compile(r"^[ \t]{0,3}(#{2,6})[ \t]*(.*?)[ \t]*$")
_BOLD_LINE = re.compile(r"^([ \t]*)\*\*(.+?)\*\*[ \t]*$")
_BOLD_NUMBERED = re.compile(r"^([ \t]*)\*\*[ \t]*(\d+(?:[.．]\d+)+)[ \t]+(\S.*?)[ \t]*\*\*[ \t]*$")
_LEAD_ARABIC = re.compile(r"^[（(]?[ \t]*\d+(?:[.．]\d+)*[ \t]*[)）.．、:：]?[ \t]*")
_HEADING_PUNCT = re.compile(
    r"[()（）\[\]【】<>《》\-—–_:：,，.。;；、/\\|\"'“”‘’*#`~!！?？+=%&…·]+"
)
_NUMERAL_ONLY = re.compile(r"^[\d一二三四五六七八九十百零两壹贰叁肆伍陆柒捌玖拾]+$")


def _visible_heading(text: str) -> str:
    value = str(text or "")
    for char in _INVISIBLE_CHARS:
        value = value.replace(char, "")
    value = re.sub(r"\s+", "", value)
    value = _LEAD_ARABIC.sub("", value, count=1)
    return _HEADING_PUNCT.sub("", value)


def _has_heading_title(text: str) -> bool:
    visible = _visible_heading(text)
    return bool(visible) and not _NUMERAL_ONLY.match(visible)


def _drop_empty_subheadings(text: str) -> tuple[str, int]:
    body = str(text or "")
    if not body:
        return body, 0
    dropped: list[int] = []
    kept: list[str] = []
    changed = 0
    track_fence = sum(1 for line in body.splitlines() if _FENCE_LINE.match(line)) % 2 == 0
    fence = ""
    for raw in body.splitlines(keepends=True):
        core = raw.rstrip("\r\n")
        ending = raw[len(core):]
        marker = _FENCE_LINE.match(core) if track_fence else None
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            kept.append(raw)
            continue
        if fence:
            kept.append(raw)
            continue
        heading = _HEADING_LINE.match(core)
        if heading:
            depth, title = len(heading.group(1)), heading.group(2)
            while dropped and depth <= dropped[-1]:
                dropped.pop()
            if not _has_heading_title(title):
                dropped.append(depth)
                changed += 1
                continue
            if dropped:
                depth = max(2, depth - len(dropped))
                core = "#" * depth + " " + title.strip()
            kept.append(core + ending)
            continue
        bold = _BOLD_LINE.match(core)
        if bold and not _has_heading_title(bold.group(2)):
            changed += 1
            continue
        kept.append(core + ending)
    if not changed:
        return body, 0
    return "".join(kept), changed


def _strict_child_number(chapter_number: str, number: str) -> str:
    parent = [seg for seg in str(chapter_number or "").strip().split(".") if seg]
    if not parent:
        return str(number or "").strip()
    local = [seg for seg in re.split(r"[.．]", str(number or "")) if seg]
    if len(local) > len(parent):
        local = local[len(parent):]
    elif local[:len(parent)] == parent:
        local = local[len(parent):]
    while local and local[:len(parent)] == parent:
        local = local[len(parent):]
    while len(local) > 1 and local[0] == parent[-1]:
        local = local[1:]
    if not local:
        local = ["1"]
    return ".".join(parent + local)


def _rebase_bold_numbers(text: str, chapter_number: str) -> tuple[str, int]:
    label = str(chapter_number or "").strip()
    body = str(text or "")
    if not label or not body:
        return text, 0
    changed = 0
    kept: list[str] = []
    for raw in body.splitlines(keepends=True):
        core = raw.rstrip("\r\n")
        ending = raw[len(core):]
        matched = _BOLD_NUMBERED.match(core)
        if not matched:
            kept.append(raw)
            continue
        indent = matched.group(1)
        old = matched.group(2).replace("．", ".")
        title = matched.group(3)
        new = _strict_child_number(label, old)
        if new == old:
            kept.append(raw)
            continue
        kept.append("%s**%s %s**%s" % (indent, new, title, ending))
        changed += 1
    if not changed:
        return text, 0
    return "".join(kept), changed


def renumber_inline(text: str, number: str, containers: set[str] | None = None) -> tuple[str, int]:
    try:
        text, dropped = _drop_empty_subheadings(text)
        label = str(number or "").strip()
        if label in (containers or set()):
            fixed, _changed = strip_inline_numbering(text)
            return fixed, dropped
        fixed, changed = normalize_inline_numbering(text, label)
        fixed, rebased = _rebase_bold_numbers(fixed, label)
        return fixed, dropped + changed + rebased
    except Exception:  # noqa: BLE001
        return text, 0
