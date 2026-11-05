"""Whole-document writing-focus planning for the B/S body-generation flow."""

from __future__ import annotations

import json
import re
from typing import Any

from zhibiao.core.ledger import truncate_ledger

# 编排阶段注入的台账预算。台账里「唯一参数口径」可能上万字，直接按字符截断会把
# **末尾的【技术规格书要点】整节丢掉**，编排模型看不到真实参数与标准号，各节写作重点
# 只能空泛。改成按小节保留的截断（见 ledger.truncate_ledger），每节都留有余量。
LEDGER_PROMPT_BUDGET_CHARS = 15000


PLAN_SYSTEM = """你是本项目投标文件的正文总编，只负责编排，不写正文。
结合完整目录、上下级和同级关系、全书统一事实，为目录中的每一节确定唯一写作范围。
每节用1至2句话说明写什么、从哪个角度写、与相邻章节如何分工。
不得编造参数、人员、设备、品牌、型号、周期、业绩、证书、价格或承诺。
有下级章节的分支只说明总体范围和下级分工，不能重复子章节的具体措施。
相同事项只分配给一个最合适章节；只输出严格 JSON：
{"plans":[{"seq":1,"writing_focus":"..."}]}"""


def planner_prompt(chapters: list[dict[str, Any]], ledger_text: str) -> str:
    lines: list[str] = []
    for chapter in chapters:
        indent = "  " * (int(chapter.get("level") or 1) - 1)
        marker = "（分支概述）" if chapter.get("has_children") else ""
        children = "；下级：" + "、".join(chapter.get("children") or []) if chapter.get("children") else ""
        lines.append(
            f"{indent}- seq={chapter.get('seq')} {chapter.get('title')}{marker}{children}"
        )
    outline = "\n".join(lines)[:36000]
    ledger_budget = truncate_ledger(ledger_text, LEDGER_PROMPT_BUDGET_CHARS) if ledger_text else ""
    return (
        "【全书统一事实与口径】\n" + (ledger_budget or "（当前没有已确认的项目事实）")
        + "\n\n【完整目录】\n" + outline
        + "\n\n请为每个 seq 提供 writing_focus。只输出 plans 数组，不要生成章节正文。"
    )


def parse_plans(raw: str) -> dict[int, str]:
    text = str(raw or "").strip().replace(chr(96) * 3, "")
    try:
        value = json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
        except Exception:
            return {}
    entries = value.get("plans") if isinstance(value, dict) else value
    if not isinstance(entries, list):
        return {}
    plans: dict[int, str] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            seq = int(item.get("seq") or item.get("id") or 0)
        except (TypeError, ValueError):
            continue
        focus = str(item.get("writing_focus") or item.get("focus") or "").strip()
        if seq > 0 and focus:
            plans[seq] = focus[:500]
    return plans


def fallback_focus(chapter: dict[str, Any], hint: str) -> str:
    title = str(chapter.get("title") or "本节").strip()
    if chapter.get("has_children"):
        children = "、".join(chapter.get("children") or []) or "下级章节"
        return (
            f"本节作为“{title}”总述，只交代本章范围、总体原则和各部分关系；"
            f"{children}的具体措施由对应下级章节分别展开，本节不重复细节。"
        )
    siblings = "、".join(chapter.get("siblings") or []) or "相邻章节"
    return (
        f"本节聚焦“{title}”，{hint}；仅承担本节范围内的响应，与{siblings}区分内容，"
        "只使用已确认事实，缺失信息保留待填占位。"
    )
