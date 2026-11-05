"""体检打分：按证据扣分（extractable / not_in_source / note），不按「感觉」。

旧口径是 ``100 − Σ{high:25, warn:10, info:2}``：只要「没抽到」就扣，不管原文到底有没有。
原文压根没写「共 N 台」，整机台数这一项照样扣 10 分；一条 26 条的技术要求里少抽 1 条，
也按 high 一整项扣 25 分。用户的原话是「这个分数也太低了吧，但是不可以瞎弄」——
问题不在分数低，在于扣分没有证据。

这一模块只负责**算分**，不负责判断「原文有没有」（那要看原文，见
``tender_checkup.checkup_checks``）。输入是一组已经判定好的考核项，每项带
``kind`` / ``weight`` / ``passed`` / ``evidence``：

* ``extractable``（原文确实给了、应当抽到）：未通过才参与扣分；
* ``not_in_source``（原文本来没有）：**不扣分**，只显示「原文未提供」；
* ``note``（原文事实，需人工注意）：**不扣分**，只提示。

三条硬规则：

1. **没有证据就不许扣分** —— 扣分项的 ``evidence`` 必须非空（调用方保证：抽不到时
   先去原文找证据，找不到就判 ``not_in_source``，根本不进扣分池）；
2. **分母不是 100**，而是本次 ``extractable`` 项的权重之和 —— 原文没给的东西不进分母，
   所以「原文没提供整机台数」不会再把分数拉下来；
3. **权重只来自 ``CHECK_WEIGHTS``**，不散落在 if 里，且每项 ≤ 总分的 1/4。

分数：``得分 = 100 − Σ(未通过项权重) / 分母 × 100``，四舍五入取整。

单项扣分上限 ``MAX_ITEM_POINTS``（总分的 1/4）：分母只剩一两项时，按比例折开会算出
50~100 分，等于让一个维度吃掉整份分 —— 这正是旧口径（high 一项 −25）想避免的事。
触发上限时 ``evidence["capped"]`` 会写出来，界面照样显示「必须处理」。
"""
from __future__ import annotations

from typing import Any

KIND_EXTRACTABLE = "extractable"
KIND_NOT_IN_SOURCE = "not_in_source"
KIND_NOTE = "note"

MAX_SCORE = 100
# 单项扣分上限：总分的 1/4。
MAX_ITEM_POINTS = MAX_SCORE // 4

# 考核项权重表：key → (权重, 为什么是这个权重)。
#
# 权重只回答「抽不到时扣多少」，不回答「原文有没有」——后者由证据判定（见
# `tender_checkup.checkup_checks`）。约束（有测试锁死）：
#   ① 权重只出现在这张表里，不许散落在 if 里；
#   ② 每项 ≤ 总分的 1/4（25 分）；
#   ③ 全部 extractable 同时成立时分母正好 100，此时单项扣分 == 权重。
CHECK_WEIGHTS: dict[str, tuple[int, str]] = {
    "tech_items": (18, "技术要求条目是「逐条响应」「参数对照表行数」的唯一来源，抽不到只能靠模型回忆；"
                        "但它再高就会让一条掩盖其它所有项，所以封顶在 18"),
    "source_text": (12, "正文文本是其余每一项的前提：字太少说明整份文件没进解析（扫描件/图片型 PDF）；"
                         "但这一项不能把别的项的分一起吃掉"),
    "cover_no": (10, "招标编号印在封面与每份格式文书上，原文有没有可以逐字核对，属硬证据"),
    "tech_params": (10, "部件参数块（参数名：值）决定参数对照表能不能写全"),
    "machine_count": (10, "整机台数是「单台×台数=总量」的唯一口径；只在原文写了数量时才计入"),
    "format_structure": (10, "投标文件格式章是成稿骨架；抽不到整章格式都要模型自写"),
    "cover_name": (8, "招标项目名称决定封面与页眉；抽不到会退回平台项目名"),
    "boundary": (8, "供货边界（由X方自备）漏写会变成我方供货范围，属废标级，但只影响那一条"),
    "outline_match": (8, "格式章骨架与当前大纲对不上时，成稿会缺法定文书；只在两侧都有时计入"),
    "cover_owner": (6, "招标人/采购人是投标函与质疑函的必填主体"),
}

# 判定顺序 = 界面展示顺序：先致命、再结构、最后细节。
CHECK_ORDER: tuple[str, ...] = (
    "source_text",
    "tech_items", "tech_params", "boundary", "machine_count",
    "format_structure", "outline_match",
    "cover_name", "cover_no", "cover_owner",
)

# 「扣分项」对外暴露的字段（必须项：key/标题/扣了多少/为什么/证据原文）。
_DEDUCTION_FIELDS: tuple[str, ...] = (
    "key", "title", "points", "reason", "evidence", "level", "weight", "share", "capped", "gate",
)


def weight_of(key: str) -> int:
    """某个考核项的权重（不在表里的一律 0，绝不临时拍一个数）。"""
    return int(CHECK_WEIGHTS.get(str(key or ""), (0, ""))[0])


def _round_half_up(value: float) -> int:
    """四舍五入取整（不用银行家舍入：0.5 一律进 1，结果可预期）。"""
    return int(float(value) + 0.5) if value >= 0 else -int(-float(value) + 0.5)


def _order(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 `CHECK_ORDER` 排稳定序：同一份输入永远得到同一个分数与同一种排列。"""
    def rank(item: dict[str, Any]) -> int:
        try:
            return CHECK_ORDER.index(str(item.get("key") or ""))
        except ValueError:
            return len(CHECK_ORDER)
    return sorted(checks, key=rank)


def _has_evidence(check: dict[str, Any]) -> bool:
    """这条判定带没带原文证据（扣分的唯一资格）。"""
    return bool(str(check.get("evidence") or "").strip())


def _deduction(check: dict[str, Any], points: int, share: float,
               capped: bool) -> dict[str, Any]:
    return {
        "key": str(check.get("key") or ""),
        "title": str(check.get("title") or ""),
        "points": int(points),
        "reason": str(check.get("detail") or ""),
        "evidence": str(check.get("evidence") or ""),
        "level": str(check.get("level") or "warn"),
        "weight": int(check.get("weight") or 0),
        "share": round(float(share), 1),
        "capped": bool(capped),
        "gate": bool(check.get("gate")),
    }


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item[field] for field in _DEDUCTION_FIELDS if field in item}


def score_checkup(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """按证据打分：只有 ``extractable`` 且未通过的项参与扣分。

    返回 ``score / max_score / deductions / must_fix / review / not_in_source /
    notes / skipped / evidence``，全部 JSON 可序列化、幂等。
    """
    ordered = _order([item for item in checks if isinstance(item, dict)])
    extractable = [item for item in ordered if item.get("kind") == KIND_EXTRACTABLE
                   and (item.get("passed") or _has_evidence(item))]
    # 「没有证据就不许扣分」再守一道：判定层本应保证每条扣分都带原文行，
    # 但以后新增判定时很容易忘了带证据。这种项既不扣分、也不进分母，
    # 只在 evidence.unevidenced 里留名 —— 悄悄扣分比漏检更难查。
    unevidenced = [item for item in ordered if item.get("kind") == KIND_EXTRACTABLE
                   and not item.get("passed") and not _has_evidence(item)]
    failed = [item for item in extractable if not item.get("passed")]
    denominator = sum(int(item.get("weight") or 0) for item in extractable)

    not_in_source = [{"key": str(item.get("key") or ""), "title": str(item.get("title") or ""),
                      "detail": str(item.get("detail") or ""),
                      "evidence": str(item.get("evidence") or ""),
                      "hint": str(item.get("hint") or "")}
                     for item in ordered if item.get("kind") == KIND_NOT_IN_SOURCE]
    notes = [{"key": str(item.get("key") or ""), "title": str(item.get("title") or ""),
              "detail": str(item.get("detail") or ""),
              "evidence": str(item.get("evidence") or "")}
             for item in ordered
             if item.get("kind") == KIND_NOTE and not item.get("skipped")]
    skipped = [{"key": str(item.get("key") or ""), "title": str(item.get("title") or ""),
                "detail": str(item.get("detail") or ""),
                "hint": str(item.get("hint") or "")}
               for item in ordered
               if item.get("kind") == KIND_NOTE and item.get("skipped")]

    evidence: dict[str, Any] = {
        "formula": "得分 = 100 − Σ(未通过的可抽取项权重) / 分母 × 100"
                   "（四舍五入；单项最多扣 %d 分）" % MAX_ITEM_POINTS,
        "denominator_rule": "分母 = 判定为 extractable 的考核项权重之和；"
                            "not_in_source（原文未提供）与 note（提示）不进分母",
        "denominator": int(denominator),
        "items": [{"key": str(item.get("key") or ""),
                   "weight": int(item.get("weight") or 0),
                   "why": str(item.get("weight_reason") or ""),
                   "passed": bool(item.get("passed")),
                   "evidence": str(item.get("evidence") or "")} for item in extractable],
        "excluded": [{"key": str(item.get("key") or ""), "kind": str(item.get("kind") or ""),
                      "title": str(item.get("title") or "")}
                     for item in ordered if item.get("kind") != KIND_EXTRACTABLE],
        "points": {},
        "capped": [],
        "unevidenced": [str(item.get("key") or "") for item in unevidenced],
    }

    # 正文不可用（gate）：整份文件没进解析，不是某一项抽不到 —— 直接 0 分。
    # 这里不套用单项 1/4 上限：上限是为了防止"一个维度吃掉整份分"，
    # 而 gate 命中时根本没有别的维度可以比较。
    gate = [item for item in failed if item.get("gate")]
    if gate:
        deductions = [_deduction(item, MAX_SCORE, 100.0, False) for item in gate]
        evidence["denominator"] = 0
        evidence["gate"] = "正文不可用：整份文件没有进入解析，直接 0 分，不按单项权重折算"
        evidence["points"] = {item["key"]: MAX_SCORE for item in deductions}
        return {
            "score": 0,
            "max_score": MAX_SCORE,
            "deductions": deductions,
            "must_fix": [_brief(item) for item in deductions],
            "review": [],
            "not_in_source": not_in_source,
            "notes": notes,
            "skipped": skipped,
            "unevidenced": [str(item.get("key") or "") for item in unevidenced],
            "evidence": evidence,
        }

    if denominator <= 0:
        # 原文没有给出任何可核对的抽取目标（例如只有一句公告标题）：不扣分，也不假装体检过。
        evidence["note"] = ("无可抽取项：原文没有给出任何可核对的抽取目标，"
                            "本次按满分处理，只列「原文未提供」与提示")
        return {
            "score": MAX_SCORE,
            "max_score": MAX_SCORE,
            "deductions": [],
            "must_fix": [],
            "review": [],
            "not_in_source": not_in_source,
            "notes": notes,
            "skipped": skipped,
            "unevidenced": [str(item.get("key") or "") for item in unevidenced],
            "evidence": evidence,
        }

    deductions: list[dict[str, Any]] = []
    for item in failed:
        share = int(item.get("weight") or 0) / denominator * MAX_SCORE
        rounded = _round_half_up(share)
        points = min(MAX_ITEM_POINTS, rounded)
        deductions.append(_deduction(item, points, share, rounded > MAX_ITEM_POINTS))

    total = sum(item["points"] for item in deductions)
    score = max(0, MAX_SCORE - total)
    evidence["points"] = {item["key"]: item["points"] for item in deductions}
    evidence["capped"] = [item["key"] for item in deductions if item["capped"]]
    return {
        "score": int(score),
        "max_score": MAX_SCORE,
        "deductions": deductions,
        "must_fix": [_brief(item) for item in deductions if item["level"] == "high"],
        "review": [_brief(item) for item in deductions if item["level"] == "warn"],
        "not_in_source": not_in_source,
        "notes": notes,
        "skipped": skipped,
        "unevidenced": [str(item.get("key") or "") for item in unevidenced],
        "evidence": evidence,
    }
