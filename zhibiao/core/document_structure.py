"""Pure export-only heading normalization; never rewrite prose or table cells.

The outline owns the chapter number. Body headings own relative depth, not an
independent numbering scheme. Leading title echoes are removed *before* depth is
measured so an echoed parent cannot create an invisible numbering level.
"""
from __future__ import annotations

import re

from zhibiao.core.chapter_rules import SUBHEADING_NUMBER_RE

_MARKDOWN = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_BARE = re.compile(r"^[ \t]{0,3}(\d+(?:[.．]\d+)+)[ \t]+(.+?)[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_SENTENCE = re.compile(r"[。！？；;!?，,：:]|[.]$")
_IDENTIFIER = re.compile(r"^(?:[A-Z]{1,8}[-_/]\d|\d|[Φφ]|(?:mm|MPa|kN|kg|℃|%)(?:\b|$))")


def _without_number(text: str) -> str:
    """Strip only an explicit heading prefix, not '3 operating modes'."""
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
    """Return Markdown depth/text or a conservative bare dotted heading.

    A decimal number alone, ordered-list item, sentence, model identifier, table
    row, quote or indented code is not an implicit heading. Ambiguous lines are
    left as paragraphs rather than guessed into the document outline.
    """
    matched = _MARKDOWN.match(line)
    if matched:
        return len(matched.group(1)), matched.group(2), ""
    matched = _BARE.match(line)
    if not matched:
        return None
    label, text = matched.groups()
    if (not text or len(text) > 64 or _SENTENCE.search(text)
            or _IDENTIFIER.match(text) or "|" in text or "{{" in text
            or not re.search(r"[A-Za-z\u3400-\u9fff]", text)):
        return None
    return 0, text, label.replace("．", ".")


def normalize_export_structure(content: str, title: str, chapter_number: str = "", *,
                               container: bool = False) -> str:
    """Return Markdown with title echoes removed and contiguous heading depths.

    This is a render-time copy, never a database write. Only recognized standalone
    headings change. Fenced code and every non-heading line remain verbatim.
    Container overviews have unnumbered subheads to avoid colliding with their
    actual outline children. The operation is idempotent.
    """
    lines = str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    key = _title_key(title)
    # Only leading title echoes: do not delete a later section just because its
    # title happens to equal the chapter title.
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
            # No outline anchor (unnumbered chapter or direct render): keep the
            # author's own heading text instead of inventing a numbering scheme.
            clean = _without_number(text) if number else text
        else:
            clean = text if number else (old_number + " " + text)
            requested = (len(old_number.split(".")) - len(number.split("."))
                         if number and old_number.startswith(number + ".")
                         else len(old_number.split(".")) - bare_min + 1)
        # Only actual preceding headings can be parents. Tracking their source
        # depths also keeps repeated skipped levels as siblings, not a staircase.
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
