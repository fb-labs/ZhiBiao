"""跨章数值口径冲突检测：**只定位、只告警，不改写、不阻断**。

用户实测的硬伤：第 12 节写「本项目履约保证金按合同含税总金额的 **10%** 提交」，
第 31 节前附表写「履约保证金的金额：中标含税金额的 **2%**」——
同一口径两个值，评标人一眼就能看见，而此前的检查体系里没有任何一项会发现它。

本模块只做一件事：把**明确的商务口径**（白名单）逐章抽出来，按口径键 + 限定语分桶，
同一桶里出现两个以上不同数值就报一条「同一口径出现不同数值，请核对」，
并带上命中的章节与原文短句。**绝不替你选哪个值**：不替换、不删句、不改写。

三条硬纪律（宁可漏报，不可误报）：

1. **口径白名单，宁严勿宽**。只认下面 ``_SPECS`` 里列出的商务口径。
   技术参数（MPa / kN / mm）、数量（1 套 / 2 台）、页码、章节号**一律不参与**：
   它们的单位不在取值白名单里，所以连候选都不会产生 —— 不是「先抽再过滤」，
   而是从一开始就抽不到。
2. **限定语不同不算冲突**。「含税 / 不含税」「单台 / 整机」属于不同口径，
   分桶后只在同一桶内比较；跨桶一律不报。**「没说」与「说了」也分属不同桶**：
   「按合同总金额的 10%」与「按合同含税总金额的 2%」不比较 —— 这是有意的漏报，
   因为「没说」可能只是没写全，替它认定口径会把不同基数硬凑成一条假冲突。
3. **定位不到就不报**。没有章节标题或章序、或原文短句取不到的，直接丢弃。

性能：逐章单遍扫描（每个口径一条正则），分桶用字典，没有全数值两两比较。
32 章 106k 字实测 < 0.1 s（见 tests/test_value_consistency.py 的性能用例）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ── 原文短句上限：与检查报告里的 evidence 口径一致（≤60 字）──
MAX_QUOTE = 60

# 口径词与取值之间允许的最大字符数。表格里会夹「的金额：」「形式：」这类中间词，
# 但间距必须收紧：间距里一旦出现别的数字或别的口径词就丢弃（见 _gap_is_clean）。
MAX_GAP = 18
# 取限定语的窗口（含税、单台这些修饰词可能在口径词之前或取值之后）
_QUALIFIER_WINDOW = 12

_FULLWIDTH = str.maketrans("０１２３４５６７８９％", "0123456789%")

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}

# ── 取值白名单：只有这三种写法会被抽取，别的东西（MPa/kN/mm/套/台/页）天然抽不到 ──
_VALUE_RATIO = (
    r"(?:"
    r"\d+(?:\.\d+)?\s*%"
    r"|百分之\s*(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千点]{1,6})"
    r"|千分之\s*(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千点]{1,6})"
    r"|万分之\s*(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千点]{1,6})"
    r")"
)
_VALUE_DURATION = (
    r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)\s*"
    r"(?:个)?\s*(?:工作日|日历天|天|日|个月|月|年)"
)
_VALUE_AMOUNT = (
    r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)\s*(?:万元|元)"
)

# 口径词与取值之间不允许出现的东西：句读、换行、数字。
# 排除数字是关键 —— 间距里一旦有别的数字，说明这个取值属于上一句别的口径。
_GAP = r"[^。；;，,\n\d]{0,%d}?" % MAX_GAP

# 任何口径词出现在间距里都视为「取值归属不明」，整条候选丢弃。
_KEYWORDS_ALL = re.compile(
    r"履约保证金|履约担保|投标保证金|质保金|质量保证金|保修金|质保期|质量保证期"
    r"|保修期|交货期|交付期|供货期|工期|完成期限|账期|违约金|违约罚则"
    r"|随机备件|随机易损件|零整比|预付款|进度款|到货款|验收款"
)


def _pattern(keyword: str, value: str, backward: bool = False) -> re.Pattern[str]:
    """口径词 + 间距 + 取值（或反向：取值 + 间距 + 口径词）。

    不用「外层无界重复 + 内层可变空白」的形状，间距是**有界**的 `{0,18}`，
    不存在多义切分（tests/test_regex_no_catastrophic_backtracking.py 的防线）。
    """
    if backward:
        return re.compile(r"(?P<v>%s)%s(?P<k>%s)" % (value, _GAP, keyword))
    return re.compile(r"(?P<k>%s)%s(?P<v>%s)" % (keyword, _GAP, value))


class _Spec:
    """一个口径：键、中文名、允许的取值类型，锚定它的正则，以及间距禁用字。"""

    __slots__ = ("key", "label", "kinds", "patterns", "reject_gap")

    def __init__(self, key: str, label: str, kinds: tuple[str, ...],
                 patterns: tuple[re.Pattern[str], ...],
                 reject_gap: tuple[str, ...] = ()) -> None:
        self.key = key
        self.label = label
        self.kinds = kinds
        self.patterns = patterns
        self.reject_gap = reject_gap


# 付款环节各自是一个口径：「预付款 30%」与「到货款 60%」不是同一件事，
# 不同环节的数值天然不同，混在一个键里必然误报，所以按环节分键。
_PAY_STAGES: tuple[tuple[str, str, str], ...] = (
    ("预付款比例", "预付款比例", r"预付款"),
    ("进度款比例", "进度款比例", r"进度款"),
    ("到货款比例", "到货款比例", r"到货款"),
    ("验收款比例", "验收款比例", r"验收款"),
    ("结算款比例", "结算款比例", r"结算款|尾款"),
)


# 口径白名单。**这张表就是「宁严勿宽」的全部内容**：
#   · 履约保证金 / 投标保证金 —— 比例、金额或期限；
#   · 质量保证金（质保金）比例 —— 只认比例（「质保金」还常指一笔钱，认金额会跟质保期混）；
#   · 质保期 / 交货期 / 工期 / 账期 —— 只认期限；
#   · 预付款 / 进度款 / 到货款 / 验收款 / 结算款 比例 —— 按付款环节分键；
#   · 违约金（罚则）比例 / 随机备件比例 / 零整比上限 —— 只认比例。
# 没有列入的口径（价格、数量、技术参数、页码、章节号）永远不产生候选。
_SPECS: tuple[_Spec, ...] = (
    _Spec("履约保证金", "履约保证金", ("ratio", "duration", "amount"),
          (_pattern(r"履约保证金|履约担保", _VALUE_RATIO),
           _pattern(r"履约保证金|履约担保", _VALUE_DURATION),
           _pattern(r"履约保证金|履约担保", _VALUE_AMOUNT))),
    _Spec("投标保证金", "投标保证金", ("ratio", "amount"),
          (_pattern(r"投标保证金", _VALUE_RATIO),
           _pattern(r"投标保证金", _VALUE_AMOUNT))),
    _Spec("质量保证金比例", "质量保证金（质保金）比例", ("ratio",),
          (_pattern(r"质保金|质量保证金|保修金", _VALUE_RATIO),
           _pattern(r"质保金|质量保证金|保修金", _VALUE_RATIO, backward=True))),
    # 「质保期满后 30 日内更换」说的是保修期**结束之后**的响应时限，不是质保期本身；
    # 间距里出现「满/后」就丢弃，宁可漏报也不把响应时限当质保期。
    _Spec("质保期", "质保期", ("duration",),
          (_pattern(r"质保期|质量保证期|保修期", _VALUE_DURATION),), ("满", "后")),
    _Spec("交货期", "交货期", ("duration",),
          (_pattern(r"交货期|交付期|供货期", _VALUE_DURATION),)),
    _Spec("工期", "工期", ("duration",),
          (_pattern(r"工期|完成期限", _VALUE_DURATION),)),
    _Spec("账期", "账期", ("duration",),
          (_pattern(r"账期", _VALUE_DURATION),)),
    _Spec("违约金比例", "违约金（罚则）比例", ("ratio",),
          (_pattern(r"违约金|违约罚则", _VALUE_RATIO),
           _pattern(r"违约金|违约罚则", _VALUE_RATIO, backward=True))),
    _Spec("随机备件比例", "随机备件比例", ("ratio",),
          (_pattern(r"随机(?:备件|易损件)", _VALUE_RATIO),
           _pattern(r"随机(?:备件|易损件)", _VALUE_RATIO, backward=True))),
    _Spec("零整比上限", "零整比上限", ("ratio",),
          (_pattern(r"零整比", _VALUE_RATIO),)),
) + tuple(
    _Spec(key, label, ("ratio",), (_pattern(keyword, _VALUE_RATIO),))
    for key, label, keyword in _PAY_STAGES
)


# ══════════════════════ 归一 ══════════════════════

def _cn_to_number(text: str) -> float | None:
    """中文数字 → 数值。只覆盖商务条款里会出现的写法（≤ 万位、可带小数）。"""
    text = str(text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    head, dot, tail = text.partition("点")
    if dot:
        if not re.fullmatch(r"[零一二三四五六七八九]+", tail or ""):
            return None
        base = _cn_to_number(head)
        if base is None:
            return None
        digits = "".join(str(_CN_NUM[ch]) for ch in tail)
        return float("%d.%s" % (int(base), digits))
    total = 0
    number = 0
    seen = False
    for char in text:
        if char in _CN_NUM:
            number = _CN_NUM[char]
            seen = True
        elif char in "十百千":
            multiplier = {"十": 10, "百": 100, "千": 1000}[char]
            total += (number or 1) * multiplier
            number = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return float(total + number)


def _normalize(kind: str, raw: str) -> tuple[str, str] | None:
    """原始写法 → (单位类, 归一值)。单位类不同的取值互不比较（宁漏不错）。"""
    text = str(raw or "").translate(_FULLWIDTH).strip()
    if not text:
        return None
    if kind == "ratio":
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", text)
        if match:
            return "比例", "%g%%" % float(match.group(1))
        for prefix, factor in (("百分之", 1.0), ("千分之", 0.1), ("万分之", 0.01)):
            if text.startswith(prefix):
                number = _cn_to_number(text[len(prefix):])
                if number is None:
                    return None
                return "比例", "%g%%" % round(number * factor, 6)
        return None
    if kind == "amount":
        match = re.fullmatch(r"(.+?)\s*(万元|元)", text)
        if not match:
            return None
        number = _cn_to_number(match.group(1))
        if number is None:
            return None
        return "金额", "%g元" % round(number * (10000 if match.group(2) == "万元" else 1), 6)
    if kind == "duration":
        match = re.fullmatch(r"(.+?)\s*(?:个)?\s*(工作日|日历天|天|日|个月|月|年)", text)
        if not match:
            return None
        number = _cn_to_number(match.group(1))
        if number is None:
            return None
        unit = match.group(2)
        # 「30 日」与「30天」是同一口径的两种写法 → 统一到「天」；
        # 「10 个工作日」与「10个工作日」→ 统一到「工作日」；年 → 月。
        if unit in ("日历天", "天", "日"):
            return "天", "%g天" % number
        if unit == "工作日":
            return "工作日", "%g个工作日" % number
        if unit == "年":
            return "月", "%g个月" % round(number * 12, 6)
        return "月", "%g个月" % number
    return None


def _kind_of(raw: str) -> str:
    text = str(raw or "").translate(_FULLWIDTH)
    if re.search(r"%|百分之|千分之|万分之", text):
        return "ratio"
    if re.search(r"元", text):
        return "amount"
    return "duration"


# ══════════════════════ 限定语分桶 ══════════════════════

def _qualifiers(context: str) -> tuple[str, str]:
    """(税口径, 计量口径) —— 限定语不同的两次提及**不算冲突**。

    含税/不含税、单台/整机是不同的口径，硬把它们放一起比就是误报；
    这里只做归类，归类不同就不比（宁可漏报）。没写限定语的归空桶，
    空桶不与任何明确桶混比 —— 见模块文档「三条硬纪律」第 2 条。
    """
    tax = ""
    if re.search(r"不含税|未税|不含增值税|税前", context):
        tax = "不含税"
    elif "含税" in context:
        tax = "含税"
    scope = ""
    if "单台" in context:
        scope = "单台"
    elif re.search(r"整机|整套|全套", context):
        scope = "整机"
    return tax, scope


def _gap_is_clean(gap: str) -> bool:
    """间距里出现别的口径词 ⇒ 这个取值归属不明，丢弃。"""
    return _KEYWORDS_ALL.search(gap) is None


def _quote(content: str, head: int, tail: int) -> str:
    """命中处原文短句（≤60 字）。保留原文，只把换行/连续空白压成一个空格。

    取值后面再顺延几个字，让「10%提交」这种半句话读完整；但遇到句读、
    空白或下一个口径词就停 —— 不能把下一句别的口径切半个词进来。
    """
    extra: list[str] = []
    for char in content[tail:tail + 8]:
        if char in "。；;，,、\n\r \t\u3000：":
            break
        extra.append(char)
    candidate = "".join(extra)
    found = _KEYWORDS_ALL.search(candidate)
    if found:
        candidate = candidate[:found.start()]
    text = re.sub(r"\s+", " ", content[max(0, head):max(tail, head)] + candidate).strip()
    return text[:MAX_QUOTE]


# ══════════════════════ 扫描 ══════════════════════

def scan_value_mentions(chapters: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """逐章抽出白名单口径的取值（含定位），供分桶与调试使用。

    每条：{key,label,kind,unit,normalized,value,tax,scope,chapter_id,chapter_title,
           chapter_seq,quote,offset}
    定位不到（没有章标题/章序、取不到原文短句）的直接丢弃 —— 宁漏不错。
    """
    mentions: list[dict[str, Any]] = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("title") or "").strip()
        try:
            seq = int(chapter.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if not title or seq <= 0:          # 定不到章就不报
            continue
        content = str(chapter.get("content") or "").translate(_FULLWIDTH)
        if not content:
            continue
        chapter_id = str(chapter.get("id") or "")
        seen: set[tuple[str, int]] = set()
        for spec in _SPECS:
            for pattern in spec.patterns:
                for match in pattern.finditer(content):
                    raw = match.group("v")
                    gap = content[match.end("k"):match.start("v")]
                    if not _gap_is_clean(gap):
                        continue
                    if any(char in gap for char in spec.reject_gap):
                        continue
                    kind = _kind_of(raw)
                    if kind not in spec.kinds:
                        continue
                    normalized = _normalize(kind, raw)
                    if normalized is None:
                        continue
                    unit, value = normalized
                    # 同一个取值被「正向」与「反向」两条规则各命中一次时只留一条。
                    dedupe = (spec.key, match.start("v"))
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    window = content[max(0, match.start("k") - _QUALIFIER_WINDOW):
                                     min(len(content), match.end("v") + _QUALIFIER_WINDOW)]
                    tax, scope = _qualifiers(window)
                    head = min(match.start("k"), match.start("v"))
                    quote = _quote(content, head, max(match.end("k"), match.end("v")))
                    if not quote:
                        continue
                    mentions.append({
                        "key": spec.key, "label": spec.label, "kind": kind,
                        "unit": unit, "normalized": value, "value": raw.strip(),
                        "tax": tax, "scope": scope,
                        "chapter_id": chapter_id, "chapter_title": title,
                        "chapter_seq": seq, "quote": quote,
                        "offset": head,
                    })
    return mentions


def conflict_message() -> str:
    """中性文案：只陈述事实，不判定、不给出结论值。"""
    return "同一口径出现不同数值，请核对"


def scan_value_conflicts(chapters: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """跨章数值口径冲突：同一口径 + 同一限定语 + 同一单位类下出现不同数值。

    返回**扁平的命中项**，每条都带完整定位：:
        {key, label, message, values, chapter_id, chapter_title, chapter_seq,
         quote, value}

    判定顺序固定（口径白名单顺序 → 限定语 → 归一值 → 章序 → 位置），
    同一份稿跑两遍结果完全一致（幂等）。
    """
    mentions = scan_value_mentions(chapters)
    buckets: dict[tuple[str, str, str, str], dict[str, list[dict[str, Any]]]] = {}
    for mention in mentions:
        bucket = buckets.setdefault(
            (mention["key"], mention["tax"], mention["scope"], mention["unit"]), {})
        bucket.setdefault(mention["normalized"], []).append(mention)

    order = {spec.key: index for index, spec in enumerate(_SPECS)}
    labels = {spec.key: spec.label for spec in _SPECS}
    conflicts: list[dict[str, Any]] = []
    for bucket_key in sorted(buckets, key=lambda k: (order.get(k[0], 99), k[1], k[2], k[3])):
        values = buckets[bucket_key]
        if len(values) < 2:
            continue
        # 同一章里的多个取值通常是不同对象（单台配置、易损件），不是跨章冲突。
        if len({m["chapter_seq"] for group in values.values() for m in group}) < 2:
            continue
        key = bucket_key[0]
        # 组内提及按「章序 → 位置」排，原文写法按首次出现顺序列出（10% 在前、2% 在后），
        # 不排序、不改写；排序只为保证同一份稿每次得到同一个结果（幂等）。
        ordered = sorted((m for group in values.values() for m in group),
                         key=lambda m: (m["chapter_seq"], m["offset"]))
        raw_values = list(dict.fromkeys(m["value"] for m in ordered))
        for mention in ordered:
            conflicts.append({
                "key": key, "label": labels.get(key, key),
                "message": conflict_message(),
                "values": raw_values,
                "chapter_id": mention["chapter_id"],
                "chapter_title": mention["chapter_title"],
                "chapter_seq": mention["chapter_seq"],
                "quote": mention["quote"],
                "value": mention["value"],
                "normalized": mention["normalized"],
            })
    return conflicts


def describe_conflicts(conflicts: list[dict[str, Any]], limit: int = 3) -> str:
    """把命中项压成一句话，供检查报告 detail 用（中性文案，不选值）。"""
    if not conflicts:
        return ""
    grouped: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for item in conflicts:
        grouped.setdefault((str(item.get("key") or ""),
                            tuple(item.get("values") or ())), []).append(item)
    parts: list[str] = []
    for (key, _values), group in list(grouped.items())[:limit]:
        spans = "、".join(
            "%s（第 %s 节「%s」）" % (item.get("value"), item.get("chapter_seq"),
                                    item.get("chapter_title"))
            for item in group[:3])
        parts.append("%s %s" % (group[0].get("label") or key, spans))
    text = conflict_message() + "：" + "；".join(parts)
    if len(grouped) > limit:
        text += "；另有 %d 处同类提示" % (len(grouped) - limit)
    return text