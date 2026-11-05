from __future__ import annotations

import re
from collections.abc import AsyncIterator

from zhibiao.generation.prompts import chapter_messages, continue_messages
from zhibiao.llm import ChatClient
from zhibiao.models import Analysis, BidSection, Tender

END_PUNCT = frozenset("。！？；：.!?;:）)】”\"'…—")
BAD_TAILS = ("如下", "包括", "其中", "例如", "：", ":", "、", "（", "(", "分别", "首先", "一是")
FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")


def cap_tokens(target_words: int, configured: int = 6000) -> int:
    target = max(300, int(target_words or 0))
    cap_chars = int(target * 1.35)
    # 商汤 GLM 等推理模型会把 reasoning tokens 计入 max_tokens，
    # 预算过低会导致正文只输出 1-2 个字；这里保留足够的推理+正文空间。
    return max(3600, min(configured, int(cap_chars / 1.45) + 200))


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[\W_]+", "", text or "")
    return {clean[index : index + 2] for index in range(max(0, len(clean) - 1))}


def project_facts(tender: Tender, analysis: Analysis | None) -> str:
    lines = [
        f"项目名称：{tender.title}",
        f"项目编号：{tender.code}",
        f"采购人：{tender.buyer or '未填写'}",
        f"预算金额：{tender.budget or '未填写'}",
        f"投标截止：{tender.deadline or '未填写'}",
    ]
    if not analysis:
        return "\n".join(lines)
    if analysis.overview:
        lines.append(f"项目总览：{analysis.overview}")
    for item in analysis.dimensions or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        value = str(item.get("value") or "")
        if label and value and value != "未明确":
            lines.append(f"{label}：{value}")
    requirements = [str(item) for item in (analysis.requirements or []) if str(item).strip()]
    if requirements:
        lines.append("实质性要求：\n" + "\n".join(f"- {item}" for item in requirements[:20]))
    risks = [
        item
        for item in (analysis.risks or [])
        if isinstance(item, dict) and str(item.get("level")) in {"blocker", "critical", "major"}
    ]
    if risks:
        lines.append("重点风险：\n" + "\n".join(f"- {item.get('title')}：{item.get('detail')}" for item in risks[:12]))
    return "\n".join(lines)[:12000]


def scoring_context(heading: str, analysis: Analysis | None) -> str:
    items = (analysis.scoring or {}).get("items") if analysis else []
    if not isinstance(items, list) or not items:
        return ""
    grams = _bigrams(heading)
    ranked = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        criteria = str(item.get("criteria") or "")
        score = len(grams & _bigrams(name + " " + criteria))
        ranked.append((score, name, criteria, item.get("points")))
    ranked.sort(key=lambda row: row[0], reverse=True)
    chosen = [row for row in ranked[:4] if row[0] > 0] or ranked[:3]
    return "\n".join(
        f"- {name}（{points or '—'}分）：{criteria[:300]}"
        for _, name, criteria, points in chosen
        if name
    )


def chapter_scope(section: BidSection, siblings: list[str]) -> str:
    title = section.heading
    if re.search(r"偏离|偏差", title):
        return "只作无偏离声明与肯定性响应，不得罗列任何偏离条目、替代措施或偏离登记表。"
    if not section.parent_key:
        return "本章为分支总述，只写范围、总体思路与下级分工，具体措施留给下级章节。"
    peers = "、".join(siblings[:10]) or "无"
    return f"本章是具体执行章节，围绕标题写可核查做法；同级章节（{peers}）各自承担内容，不重复。"


def neighbour_context(sections: list[BidSection], current: BidSection, limit: int = 3) -> str:
    written = [row for row in sections if row.position < current.position and (row.body or "").strip()]
    blocks = [f"{row.heading}：{(row.body or '').strip()[:700]}" for row in written[-limit:]]
    return "\n".join(blocks)


def _normalize(text: str) -> str:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in cleaned.splitlines():
        if FENCE.match(line):
            continue
        lines.append(line.rstrip())
    body = "\n".join(lines).strip()
    body = re.sub(r"\n{4,}", "\n\n\n", body)
    return body


def _strip_heading(body: str, heading: str) -> str:
    text = body.lstrip()
    patterns = (f"# {heading}", f"## {heading}", f"### {heading}", heading)
    for pattern in patterns:
        if text.startswith(pattern):
            text = text[len(pattern) :].lstrip(" \n：:")
    return text


def needs_continue(text: str, target_words: int) -> bool:
    body = str(text or "").strip()
    if len(body) < 50:
        return False
    if len(body) < int(target_words * 0.55):
        return True
    tail = body[-1:]
    if tail in END_PUNCT:
        return any(body.endswith(item) for item in BAD_TAILS)
    return tail not in END_PUNCT


def merge_overlap(base: str, addition: str) -> str:
    left = str(base or "").rstrip()
    right = str(addition or "").lstrip()
    if not right:
        return left
    for size in range(min(120, len(left), len(right)), 15, -1):
        if left[-size:] == right[:size]:
            return left + right[size:]
    if right.startswith(left[-20:]):
        return left + right[20:]
    separator = "\n" if left and right and not left.endswith("\n") else ""
    return left + separator + right


def finalize(text: str, heading: str, target_words: int) -> str:
    body = _normalize(_strip_heading(text, heading))
    limit = max(500, int(target_words * 1.7))
    if len(body) > limit:
        cut = body[:limit]
        boundary = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("\n"))
        body = cut[: boundary + 1] if boundary > limit * 0.7 else cut
    return body.strip()


async def generate_body(
    client: ChatClient,
    tender: Tender,
    analysis: Analysis | None,
    section: BidSection,
    sections: list[BidSection],
) -> str:
    facts = project_facts(tender, analysis)
    siblings = [
        row.heading
        for row in sections
        if row.parent_key == section.parent_key and row.id != section.id
    ]
    messages = chapter_messages(
        tender.title,
        section.heading,
        section.intent,
        chapter_scope(section, siblings),
        section.target_words,
        facts,
        scoring_context(section.heading, analysis),
        "、".join(siblings),
        neighbour_context(sections, section),
    )
    text = await client.chat(messages, max_tokens=cap_tokens(section.target_words))
    rounds = 0
    while needs_continue(text, section.target_words) and rounds < 2:
        addition = await client.chat(
            continue_messages(section.heading, text[-900:], section.target_words, len(text)),
            max_tokens=cap_tokens(section.target_words),
        )
        merged = merge_overlap(text, addition)
        if merged == text:
            break
        text = merged
        rounds += 1
    return finalize(text, section.heading, section.target_words)


async def stream_body(
    client: ChatClient,
    tender: Tender,
    analysis: Analysis | None,
    section: BidSection,
    sections: list[BidSection],
) -> AsyncIterator[str]:
    facts = project_facts(tender, analysis)
    siblings = [
        row.heading
        for row in sections
        if row.parent_key == section.parent_key and row.id != section.id
    ]
    messages = chapter_messages(
        tender.title,
        section.heading,
        section.intent,
        chapter_scope(section, siblings),
        section.target_words,
        facts,
        scoring_context(section.heading, analysis),
        "、".join(siblings),
        neighbour_context(sections, section),
    )
    chunks: list[str] = []
    async for delta in client.stream(messages, max_tokens=cap_tokens(section.target_words)):
        chunks.append(delta)
        yield delta
    text = "".join(chunks)
    rounds = 0
    while needs_continue(text, section.target_words) and rounds < 2:
        addition = await client.chat(
            continue_messages(section.heading, text[-900:], section.target_words, len(text)),
            max_tokens=cap_tokens(section.target_words),
        )
        merged = merge_overlap(text, addition)
        if merged == text:
            break
        tail = merged[len(text) :]
        text = merged
        rounds += 1
        if tail:
            yield tail
    final = finalize(text, section.heading, section.target_words)
    if final.startswith(text):
        rest = final[len(text) :]
        if rest:
            yield rest
