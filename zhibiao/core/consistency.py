"""跨章节一致性：产出侧事实台账 + **只告警、不自动改写**。

当前实现（与代码一致，勿再按旧注释理解）：
- 程序抽取每章事实（数值、期限、专利号等）→ 归集台账 → 同一标签出现多个取值时告警；
- `_pick_target` 无条件返回 None：标签+数值没有绑定设备对象与起算条件，不足以授权
  全局替换，硬改会把不同主体/不同保修对象的事实合并，所以**一律只提示人工核对**；
- 因此 `_rewrite_value` / `_looks_like_collateral_damage` / `_authoritative_values` /
  `reverted` 这些最小编辑与回退安全网在当前版本**不可达**（保留代码以备将来接入带对象
  来源的结构化口径），调用方 generation_runner 也直接给空 repairs。
- 口头禅：宁可漏改不可错改，丢正文是废标级。

已停用的旧规则（历史记录）：
  1. 权威文本（authoritative_text）里有唯一取值 → 以它为准改写
  2. 没有权威口径时，某一取值占全部提及的 2/3 以上才归一
  3. 其余（含并列、多值、专利号）一律不动，只保留告警
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


# 全角数字先归一；单位必须「最长优先」匹配：否则「90日历天」会先匹配成「90日」，
# 同一事实出现「90日」与「90个日」两个台账值 → 报假冲突。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_DIGIT_CHARS = r"0-9０-９一二两三四五六七八九十百"
_VALUE = (r"([" + _DIGIT_CHARS + r"]+\s*(?:个)?\s*"
          r"(?:个日历天|日历天|工作日|个月|小时|分钟|月|年|天|日|元|万元|%|％)"
          r"|终身|永久|长期)")
_TERMS = {
    # 「质保12个月」是正文最常见写法（招标 fixture 就长这样），必须与「质保期」同等识别；
    # 但「质保金」是金额，不是期限，用否定前瞻排除。
    "质保/保修期限": re.compile(r"(?:质量保证期|质保期|质保(?!金)|保修期|保修(?!金))[^0-9０-９一二两三四五六七八九十百\n，。；]{0,16}?" + _VALUE),
    "服务期限": re.compile(r"(?:服务期|服务期限|履约期限)[^0-9０-９一二两三四五六七八九十百\n，。；]{0,16}?" + _VALUE),
    "工期/交付期限": re.compile(r"(?:工期|交货期|交付期|完成期限)[^0-9０-９一二两三四五六七八九十百\n，。；]{0,16}?" + _VALUE),
    "响应时限": re.compile(r"(?:响应时间|响应时限|到场时间|到场时限)[^0-9０-９一二两三四五六七八九十百\n，。；]{0,16}?" + _VALUE),
    "投标有效期": re.compile(r"(?:投标有效期|响应有效期)[^0-9０-９一二两三四五六七八九十百\n，。；]{0,16}?" + _VALUE),
}

# 这些标签**永不自动改写**：专利号/证书号属于身份性数据，写错就是造假，
# 只能靠出件门禁拦下让人工确认，绝不能由程序替换成另一个号。
_NEVER_REWRITE: frozenset[str] = frozenset({"专利号"})

# 真实专利号（ZL201820123456.7）与掩码号（ZL2018XXXXXX）都要能抽到：
# 旧正则只认掩码，导致真实专利号跨章不一致永远发现不了。
_PATENT_RE = re.compile(
    r"ZL\s*\d{9,}\.?\d?"
    r"|ZL?\d{4}\d{2}X{2,}[0-9X.]*"
    r"|\b[A-Z]{2}\d{4,}X{3,}"
)

# 权威文本（招标文件解读台账）是「名称：值」的列表形式，冒号会挡住上面的取值正则，
# 所以为权威抽取单独放宽分隔符 —— 否则 `- 质保期：5年` 永远取不到口径。
_AUTH_TERMS: dict[str, re.Pattern[str]] = {
    label: re.compile(pattern.pattern.replace(r"[^0-9０-９一二两三四五六七八九十百\n，。；]{0,16}?", r"[^\n，。；]{0,16}"))
    for label, pattern in _TERMS.items()
}

_MAX_WARNINGS = 20


def _looks_like_collateral_damage(original: str, updated: str) -> bool:
    """修复幅度安全网（抄 80 `bid_consistency.py`）：

    修复只应「改正」，不该「删内容」。一轮砍掉原文 40% 以上判定为规则误伤，
    整章回退原文 —— 宁可漏改不可错改，丢正文是废标级。
    """
    return len(original) >= 100 and len(updated) < len(original) * 0.60


def _norm(value: str) -> str:
    # 全角数字归一：招标文件里「２４个月」与「24个月」是同一事实
    text = str(value or "").translate(_FULLWIDTH_DIGITS)
    text = re.sub(r"\s+", "", text)
    # 「90个日历天」与「90日历天」是同一事实的两种写法：量词「个」不参与数值比较
    return re.sub(r"个(?=日历天)", "", text)


# ══════════════════════ 1. 产出侧事实台账 ══════════════════════

def extract_chapter_facts(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐章抽取可核查事实，形成「产出侧台账」。

    与 `scan_cross_chapter_conflicts` 的区别：本函数产出**结构化台账**（可按章落库、
    事后审计），并额外纳入专利号等身份性数据 —— 它们跨章不一致同样是废标级信号。
    """
    ledger: list[dict[str, Any]] = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        content = str(chapter.get("content") or "")
        items: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for label, pattern in _TERMS.items():
            for match in pattern.finditer(content):
                value = _norm(match.group(1))
                key = (label, value)
                if value and key not in seen:
                    items.append({"label": label, "value": value})
                    seen.add(key)
        for match in _PATENT_RE.finditer(content):
            value = _norm(match.group(0))
            key = ("专利号", value)
            if value and key not in seen:
                items.append({"label": "专利号", "value": value})
                seen.add(key)
        ledger.append({
            "seq": chapter.get("seq"),
            "title": str(chapter.get("title") or ""),
            "facts": items,
        })
    return ledger


def conflicts_from_facts(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """用产出侧台账比对跨章数值（含专利号）。输出与 scan 同构的 warnings。"""
    found: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in ledger or []:
        title = str(row.get("title") or "")
        for item in row.get("facts") or []:
            label = str(item.get("label") or "")
            value = str(item.get("value") or "")
            if label and value and title and title not in found[label][value]:
                found[label][value].append(title)
    warnings: list[dict[str, Any]] = []
    for label, values in found.items():
        if len(values) < 2:
            continue
        # 同一章内的多个取值（不同设备对象/易损件）本就该并列保留，不是跨章冲突；
        # 只有取值分布在 ≥2 个不同章节时才是真正的跨章不一致。
        chapters = {name for titles in values.values() for name in titles if name}
        if len(chapters) < 2:
            continue
        warnings.append({
            "label": label,
            "values": [
                {"value": value, "chapters": titles[:8]}
                for value, titles in values.items()
            ],
            "message": (
                f"不同章节出现多个「{label}」取值，请人工核对对象和起算条件。"
                if label in _NEVER_REWRITE else
                f"不同章节出现多个「{label}」数值，须按设备对象与起算条件核对；"
                f"没有权威口径时仅提示人工核对。"
            ),
        })
    return warnings[:_MAX_WARNINGS]


# ══════════════════════ 2. 兼容原接口 ══════════════════════

def scan_cross_chapter_conflicts(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag differing quoted values for the same label (确定性扫描，不改写)。"""
    found: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for chapter in chapters:
        title = str(chapter.get("title") or "")
        content = str(chapter.get("content") or "")
        for label, pattern in _TERMS.items():
            for match in pattern.finditer(content):
                value = _norm(match.group(1))
                if title and title not in found[label][value]:
                    found[label][value].append(title)
    warnings: list[dict[str, Any]] = []
    for label, values in found.items():
        if len(values) < 2:
            continue
        # 与 conflicts_from_facts 统一口径：同章多值不算跨章冲突；空标题章不参与。
        chapters = {name for titles in values.values() for name in titles if name}
        if len(chapters) < 2:
            continue
        warnings.append({
            "label": label,
            "values": [
                {"value": value, "chapters": titles[:8]}
                for value, titles in values.items()
            ],
            "message": (
                f"不同章节出现多个“{label}”数值，请人工核对对象和起算条件；"
                "系统不会自动改写，以免把不同主体或不同事项的事实合并。"
            ),
        })
    return warnings[:_MAX_WARNINGS]


# ══════════════════════ 3. 自动最小编辑修正 ══════════════════════

def _authoritative_values(label: str, text: str) -> list[str]:
    """从权威文本（招标文件解读台账）里取出该标签的取值。"""
    pattern = _AUTH_TERMS.get(label)
    if pattern is None or label in _NEVER_REWRITE:
        return []
    return sorted({_norm(m.group(1)) for m in pattern.finditer(text or "") if _norm(m.group(1))})


def _pick_target(label: str, values: dict[str, list[str]], authoritative_text: str) -> tuple[str | None, str]:
    """决定该标签的权威取值。返回 (取值 or None, 理由)。"""
    if label in _NEVER_REWRITE:
        return None, "身份性数据（专利号等）不做自动改写，仅提示人工确认"

    # 标签+数值没有设备对象、附件范围和起算条件，不足以授权全局替换。
    # 旧的多数投票/单个数字归一会把不同设备与保修对象合并，现保留告警交由带来源质量修订处理。
    return None, "缺少对象与起算条件的结构化来源，仅提示人工核对，不按多数或全局标签改写"



def _rewrite_value(text: str, pattern: re.Pattern[str], target: str) -> tuple[str, int]:
    """只替换命中片段里的**数值本身**，其余文字一字不动（最小编辑）。"""
    changed = 0

    def sub(match: re.Match) -> str:
        nonlocal changed
        raw = _norm(match.group(1))
        if raw == target or not raw:
            return match.group(0)
        changed += 1
        start, end = match.start(1) - match.start(0), match.end(1) - match.start(0)
        whole = match.group(0)
        return whole[:start] + target + whole[end:]

    return pattern.sub(sub, text), changed


def repair_conflicts(
    chapters: list[dict[str, Any]],
    warnings: list[dict[str, Any]] | None = None,
    authoritative_text: str = "",
) -> dict[str, Any]:
    """按权威口径做最小编辑修正，并带 80 端口的误伤安全网。

    返回 {
      "chapters": [{"id","seq","title","content","changed"}...],   # 仅含被改动的章
      "repairs": [{"seq","title","label","from","to","count"}...], # 留痕（对标 blockChanges）
      "skipped": [{"label","reason"}...],
      "reverted": [{"seq","title","reason"}...],
    }
    """
    values_map: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    rows = [c for c in (chapters or []) if isinstance(c, dict)]
    if warnings is None:
        warnings = scan_cross_chapter_conflicts(rows)
    for warning in warnings or []:
        label = str(warning.get("label") or "")
        for entry in warning.get("values") or []:
            value = str((entry or {}).get("value") or "")
            if label and value:
                for title in (entry or {}).get("chapters") or []:
                    values_map[label][value].append(str(title))

    repairs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    reverted: list[dict[str, Any]] = []
    changed_rows: list[dict[str, Any]] = []

    for label, values in values_map.items():
        if len(values) < 2:
            continue
        target, reason = _pick_target(label, values, authoritative_text)
        pattern = _TERMS.get(label)
        if target is None or pattern is None:
            skipped.append({"label": label, "reason": reason})
            continue
        for chapter in rows:
            if str(chapter.get("status") or "") == "已生成" and not str(chapter.get("content") or ""):
                continue
            original = str(chapter.get("content") or "")
            if not original:
                continue
            updated, count = _rewrite_value(original, pattern, target)
            if not count or updated == original:
                continue
            # ── 修复幅度安全网（抄 80 bid_consistency）──
            if _looks_like_collateral_damage(original, updated):
                reverted.append({
                    "seq": chapter.get("seq"), "title": chapter.get("title"),
                    "reason": f"修正后删减 {len(original)}->{len(updated)} 字超过 40%，判定误伤，整章回退原文",
                })
                continue
            old_values = sorted({_norm(m.group(1)) for m in pattern.finditer(original)})
            chapter["content"] = updated
            changed_rows.append({
                "id": chapter.get("id"), "seq": chapter.get("seq"),
                "title": chapter.get("title"), "content": updated, "changed": count,
            })
            repairs.append({
                "seq": chapter.get("seq"), "title": chapter.get("title"), "label": label,
                "from": "、".join(v for v in old_values if v != target)[:80],
                "to": target, "count": count, "reason": reason,
            })

    return {
        "chapters": changed_rows,
        "repairs": repairs,
        "skipped": skipped,
        "reverted": reverted,
    }


_PER_MACHINE_QTY = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}?)[^。；\n|]{0,12}?[|｜][ \t]*(\d{1,3})[ \t]*[|｜][ \t]*"
    r"(?:台|套|件|组)?[ \t]*/?[ \t]*单台")
_TOTAL_QTY_STMT = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,12}?)[^。；\n]{0,20}?共[ \t]*(\d{1,3})[ \t]*(?:台|套|件|组)")


def scan_document_scope(chapters: list[dict[str, Any]], tender_text: str) -> list[dict[str, Any]]:
    """全文级核对：供货边界条款有没有响应、单台数量与总量口径是否矛盾。

    只告警不改写。逐章检查管不到这类问题：14/15 条边界条款只要有一章没写就算漏，
    而「单台 2 台 × 整机 2 台 = 4 台」与别处「共 2 台」的冲突横跨两章。
    """
    from zhibiao.core.completeness import boundary_clauses, machine_count

    warnings: list[dict[str, Any]] = []
    body_all = re.sub(r"\s+", "", "".join(str(c.get("content") or "") for c in chapters or []
                                          if isinstance(c, dict)))
    if not body_all or not str(tender_text or "").strip():
        return warnings
    for clause in boundary_clauses(tender_text):
        missing = [name for name in clause["items"] if name not in body_all]
        if missing:
            warnings.append({
                "label": "供货边界条款未响应",
                "value": clause["text"][:80],
                "seq": None,
                "title": "全文",
                "message": ("招标原文「" + clause["text"][:60] + "」全文未响应："
                            + "、".join(missing[:4]) + f" 属{clause['qualifier']}件，"
                              "投标文件必须写明不在我方供货范围"),
            })
    count = machine_count(tender_text)
    per_machine: dict[str, int] = {}
    totals: dict[str, int] = {}
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        content = str(chapter.get("content") or "")
        for match in _PER_MACHINE_QTY.finditer(content):
            name = re.sub(r"^[|\s]+|[|\s]+$", "", match.group(1))
            if len(name) >= 2:
                per_machine.setdefault(name, int(match.group(2)))
        for match in _TOTAL_QTY_STMT.finditer(content):
            name = match.group(1).strip()
            if len(name) >= 2:
                totals.setdefault(name, int(match.group(2)))
    if count:
        for name, per in per_machine.items():
            total = next((value for key, value in totals.items() if name in key or key in name), None)
            if total is None or per * count == total:
                continue
            warnings.append({
                "label": "数量口径冲突",
                "value": f"{name}：{per} 台/单台 × {count} 台 = {per * count} 台，正文另写共 {total} 台",
                "seq": None,
                "title": "全文",
                "message": (f"「{name}」按「{per} 台/单台整机」× {count} 台整机 = {per * count} 台，"
                            f"但正文另有「共 {total} 台」的口径；单台配置与项目总量必须与分项报价表一致"),
            })
    return warnings


def repair_summary(result: dict[str, Any]) -> str:
    """给运行记录用的单行摘要。"""
    repairs = result.get("repairs") or []
    if not repairs:
        return "无需自动修正"
    return "自动统一 %d 处跨章口径：%s" % (
        len(repairs),
        "；".join(f"第{r.get('seq')}节 {r.get('label')} → {r.get('to')}" for r in repairs[:6]),
    )