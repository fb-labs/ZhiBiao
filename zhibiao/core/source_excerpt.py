"""Bounded, source-ordered excerpts without an implicit first-pages preference.

Selection is a retrieval aid, never a claim of complete requirement coverage.
The complete upload remains authoritative; every shortened result says so.
"""
from __future__ import annotations


def balanced_excerpt(text: str, keywords: tuple[str, ...], limit: int = 18000) -> str:
    """Keep matching clause windows distributed across the entire source.

    Preserve original lines and annotate their provenance. Large merged ranges are
    split on line boundaries before selection, so a dense opening section cannot
    consume the whole budget and conceal a late contract/appendix obligation.
    """
    raw = str(text or "")
    budget = max(0, int(limit))
    if not raw or budget <= 0:
        return ""
    lines = raw.splitlines()
    keys = tuple(str(k).casefold() for k in keywords if str(k))
    ranges: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if any(key in line.casefold() for key in keys):
            start, end = max(0, index - 2), min(len(lines), index + 5)
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))
    if not ranges:
        ranges = [(0, len(lines))]
    block_budget = max(96, min(1400, budget // 4))
    blocks: list[tuple[int, int, str]] = []
    for start, end in ranges:
        block_start = start
        chunk: list[str] = []
        size = 0
        for index in range(start, end):
            line = lines[index]
            if chunk and size + len(line) + 1 > block_budget:
                blocks.append((block_start, index, "\n".join(chunk)))
                block_start, chunk, size = index, [], 0
            # Long unwrapped paragraphs remain a single record. They are never
            # silently shortened to look like a complete clause.
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            blocks.append((block_start, end, "\n".join(chunk)))
    rendered = [f"[原文行{start + 1}-{end}]\n{body}" for start, end, body in blocks]
    full = "\n\n".join(rendered)
    if len(full) <= budget:
        return full
    warning = "[检索片段受预算限制；部分原文未展示，不代表无要求。请结合全文需求台账核验。]\n"
    if budget <= len(warning):
        return warning[:budget]
    available = budget - len(warning)
    # Farthest-point ordering: start, end, then the largest remaining gap. This
    # works even when one giant input line does not fit: other regions can fit.
    order: list[int] = []
    pending = [(0, len(blocks) - 1)]
    if blocks:
        order.append(0)
        if len(blocks) > 1:
            order.append(len(blocks) - 1)
        pending = [(1, len(blocks) - 2)]
    while pending:
        lo, hi = pending.pop(0)
        if lo > hi:
            continue
        mid = (lo + hi) // 2
        order.append(mid)
        pending.extend(((lo, mid - 1), (mid + 1, hi)))
    selected: list[int] = []
    used = 0
    for index in order:
        cost = len(rendered[index]) + (2 if selected else 0)
        if used + cost <= available:
            selected.append(index)
            used += cost
    return warning + "\n\n".join(rendered[index] for index in sorted(selected))
