"""数值保真对账：原文写明的具体数值，成稿里还在不在。

真实事故（同一份减速器检修招标，连续两版都错）：

1. 原文「齿轮的接触斑点沿齿高方向不小于45%，沿齿长方向不小于60%。接触斑点的
   分布位置应趋近齿面中部。」→ 成稿写成「沿齿长方向符合技术要求 / 按原文规定
   控制」，**60% 这个数字丢了**（9 处都丢）；
2. 原文螺栓拧紧力矩表 `M10 50 N·m … M42 3800 N·m` 共 8 档 → 成稿只写「各档按
   招标文件所列力矩值控制」，**8 个数全丢**；
3. 原文「轴应光滑完好……其椭圆度、圆锥度公差一般应小于0.03mm」→ 成稿完全没写。

本模块只做**定位**：把原文的数值要求取出来，逐个到成稿里找，找不到就报「哪个
数字没写」；反向再查成稿里出现、原文没有的数字（防幻觉）。它是纯函数，无 IO、
无模型调用、不抛异常、幂等，既不改文本也不判合规 —— 接不接线、怎么用由调用方
决定（本模块不接 generation_runner）。

比对口径：只比「数字 + 单位」归一化后的形式，不比对措辞。所以 `60%` / `60％` /
`60 %` / `不小于60%` 是同一个数值；`3800Nm` / `3800 N·m` / `3.8kN·m` 也是同一个
数值（换算到基本单位后比较）。比较词的强弱（不小于 / 不超过 / 等于）不进比对键
—— 那是合规判断，不是本模块的职责。

噪声排除：页码（`第32页`）、章节编号（`3.1.1`）、序号（`（一）`）、日期、金额里
的千分位串、表格竖线行都不是「要求数字」。表格行按单元格切分，数字只能与**同一
单元格**里的单位绑定，绝不跨竖线拼接；`|---|---|` 这类分隔行整行跳过。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "requirement_numbers",
    "response_numbers",
    "missing_numbers",
    "fabricated_numbers",
    "fidelity_gaps",
]

# ---------------------------------------------------------------------------
# 归一化：全角 → 半角（逐字符 1:1，保证索引仍可回指原文切片）
# ---------------------------------------------------------------------------

_TRANSLATION: dict[int, str] = {
    ord("％"): "%",
    ord("．"): ".",
    ord("，"): ",",
    ord("："): ":",
    ord("；"): ";",
    ord("（"): "(",
    ord("）"): ")",
    ord("　"): " ",
    ord("～"): "~",
    ord("－"): "—",
    ord("／"): "/",
    ord("＊"): "*",
    ord("｜"): "|",
    ord("−"): "—",
    ord("–"): "—",
    ord("•"): "·",
    ord("・"): "·",
    ord("∙"): "·",
}
for _i in range(10):
    _TRANSLATION[ord("０") + _i] = str(_i)
for _i in range(26):
    _TRANSLATION[ord("Ａ") + _i] = chr(ord("A") + _i)
    _TRANSLATION[ord("ａ") + _i] = chr(ord("a") + _i)


def _as_text(value: Any) -> str:
    """任何输入都变成字符串；畸形输入返回空串，绝不抛异常。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - 只有恶意 __str__ 才会走到
        return ""


def _normalize(text: Any) -> str:
    return _as_text(text).translate(_TRANSLATION)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" |")


# ---------------------------------------------------------------------------
# 单位表：别名 → 归一单位；比对时再换算到基本单位
# ---------------------------------------------------------------------------

_UNIT_CANON: dict[str, str] = {
    # 百分数 / 千分数
    "%": "%",
    "‰": "‰",
    # 长度与公差
    "mm": "mm",
    "毫米": "mm",
    "cm": "cm",
    "厘米": "cm",
    "m": "m",
    "米": "m",
    "km": "km",
    "千米": "km",
    "公里": "km",
    "μm": "μm",
    "µm": "μm",
    "um": "μm",
    "微米": "μm",
    # 力矩
    "N·m": "N·m",
    "N.m": "N·m",
    "N*m": "N·m",
    "Nm": "N·m",
    "NM": "N·m",
    "kN·m": "kN·m",
    "kN.m": "kN·m",
    "kNm": "kN·m",
    "kgf·m": "kgf·m",
    "kgf.m": "kgf·m",
    "kgfm": "kgf·m",
    "牛·米": "N·m",
    "牛米": "N·m",
    # 压力
    "MPa": "MPa",
    "兆帕": "MPa",
    "kPa": "kPa",
    "千帕": "kPa",
    "Pa": "Pa",
    "bar": "bar",
    # 时间
    "分钟": "min",
    "min": "min",
    "小时": "h",
    "h": "h",
    "秒": "s",
    "s": "s",
    # 角度 / 温度
    "°": "°",
    "度": "°",
    "℃": "℃",
    "°C": "℃",
    "摄氏度": "℃",
    # 速度 / 转速 / 流量
    "m/s": "m/s",
    "m/s²": "m/s²",
    "r/min": "r/min",
    "rpm": "r/min",
    "转/分": "r/min",
    "t/h": "t/h",
    "m³/h": "m³/h",
    "m3/h": "m³/h",
    "L/min": "L/min",
    # 功率 / 力 / 质量 / 电学
    "kW": "kW",
    "千瓦": "kW",
    "MW": "MW",
    "W": "W",
    "kg": "kg",
    "千克": "kg",
    "公斤": "kg",
    "t": "t",
    "吨": "t",
    "kN": "kN",
    "N": "N",
    "kgf": "kgf",
    "V": "V",
    "kV": "kV",
    "A": "A",
    "Hz": "Hz",
    "dB": "dB",
    "分贝": "dB",
    # 数量档位 / 倍数
    "档": "档",
    "档位": "档",
    "级": "级",
    "倍": "倍",
    "次": "次",
}

# 基本单位换算：归一单位 → (系数, 基本单位)。比对键 = 数值 × 系数 + 基本单位。
_BASE_UNITS: dict[str, tuple[float, str]] = {
    "mm": (0.001, "m"),
    "cm": (0.01, "m"),
    "m": (1.0, "m"),
    "km": (1000.0, "m"),
    "μm": (1e-6, "m"),
    "N·m": (1.0, "N·m"),
    "kN·m": (1000.0, "N·m"),
    "kgf·m": (9.80665, "N·m"),
    "MPa": (1e6, "Pa"),
    "kPa": (1e3, "Pa"),
    "bar": (1e5, "Pa"),
    "Pa": (1.0, "Pa"),
    "min": (60.0, "s"),
    "h": (3600.0, "s"),
    "s": (1.0, "s"),
    "kg": (1.0, "kg"),
    "t": (1000.0, "kg"),
    "kW": (1e3, "W"),
    "MW": (1e6, "W"),
    "W": (1.0, "W"),
    "kN": (1e3, "N"),
    "N": (1.0, "N"),
}

_UNIT_KIND: dict[str, str] = {
    "%": "percent",
    "‰": "percent",
    "mm": "length",
    "cm": "length",
    "m": "length",
    "km": "length",
    "μm": "length",
    "N·m": "torque",
    "kN·m": "torque",
    "kgf·m": "torque",
    "MPa": "pressure",
    "kPa": "pressure",
    "Pa": "pressure",
    "bar": "pressure",
    "min": "time",
    "h": "time",
    "s": "time",
    "°": "angle",
    "℃": "temperature",
    "m/s": "speed",
    "m/s²": "speed",
    "r/min": "speed",
    "t/h": "flow",
    "m³/h": "flow",
    "L/min": "flow",
    "kW": "power",
    "MW": "power",
    "W": "power",
    "kg": "mass",
    "t": "mass",
    "kN": "force",
    "N": "force",
    "kgf": "force",
    "V": "electric",
    "kV": "electric",
    "A": "electric",
    "Hz": "electric",
    "dB": "sound",
    "档": "quantity",
    "档位": "quantity",
    "级": "quantity",
    "倍": "quantity",
    "次": "quantity",
}

# 长单位优先，避免 `mm` 被 `m` 抢走、`Nm` 被 `N` 抢走。
_UNIT_ALT = "|".join(
    re.escape(alias) for alias in sorted(_UNIT_CANON, key=len, reverse=True)
)

# 数字本体：前面不能紧跟数字/小数点/逗号/字母（挡掉 M10、GB/T 25974、127,714.5），
# 后面不能紧跟数字/小数点/逗号（挡掉 3.1.1、1,000）。单位必须紧跟（允许空格）。
_TOKEN = re.compile(
    r"(?<![\d.,A-Za-z/%])"
    r"(?P<num>\d{1,6}(?:\.\d{1,6})?)"
    r"(?![\d.,])"
    r"\s*"
    r"(?P<unit>" + _UNIT_ALT + r")"
    r"(?![A-Za-z])"
)

# 区间写法 `0.03~0.05mm`：单位只挂在右边，左边这个数字要单独补回来。
_RANGE_TAIL = re.compile(
    r"(?<![\d.,A-Za-z])(\d{1,6}(?:\.\d{1,6})?)\s*[~—至]\s*$"
)

# 表格分隔行：`|---|---|`、`+----+`、`====` 这类纯线条行整行跳过。
_RULE_LINE = re.compile(r"^[\s|:+\-=_—─━]+$")

_SENTENCE_CUT = "。；;！？!?\n"
_PHRASE_CUT = "。；;！？!?\n，,：:"

# 章节编号：`第32页` 不是章节，`3.1.1` 是。
_CHAPTER_HEAD = re.compile(r"^(第[一二三四五六七八九十百千\d]+[章节部分])")
_ITEM_HEAD = re.compile(r"^(\d+)[、.．](?!\d)")
_DOTTED_HEAD = re.compile(r"^(\d+(?:\.\d+)+)(.*)$")

_PAGE_PATTERNS = (
    re.compile(r"第\s*(\d{1,4})\s*页"),
    re.compile(r"^\s*[-—–]\s*(\d{1,4})\s*[-—–]\s*$"),
    re.compile(r"^\s*PAGE\s*[:：]?\s*(\d{1,4})\s*$", re.I),
    re.compile(r"^\s*(\d{1,4})\s*/\s*(\d{1,4})\s*$"),
)


def _is_cjk(char: str) -> bool:
    return bool(char) and "\u4e00" <= char <= "\u9fff"


def _page_of(line: str) -> int | None:
    for pattern in _PAGE_PATTERNS:
        match = pattern.search(line)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _heading_label(line: str) -> str | None:
    """识别章节标题并给出可引用的条号；`0.03mm 的公差` 不是标题。"""
    body = line.strip()
    if not body:
        return None
    hashed = re.match(r"^#{1,6}\s*(.+)$", body)
    if hashed:
        body = hashed.group(1).strip()
        dotted = _DOTTED_HEAD.match(body)
        if dotted:
            return dotted.group(1)
        chapter = _CHAPTER_HEAD.match(body)
        if chapter:
            return chapter.group(1)
        item = _ITEM_HEAD.match(body)
        if item:
            return item.group(1)
        return body[:12]
    if body.startswith(("|", "```", "~~~", "[TABLE]", "[/TABLE]")):
        return None
    chapter = _CHAPTER_HEAD.match(body)
    if chapter:
        return chapter.group(1)
    item = _ITEM_HEAD.match(body)
    if item:
        return item.group(1)
    dotted = _DOTTED_HEAD.match(body)
    if not dotted:
        return None
    head = dotted.group(2).lstrip(" \t")
    if not head:
        return dotted.group(1)
    if not _is_cjk(head[0]):
        # 紧跟字母/数字/符号（`0.03mm 的公差`）不是条号
        return None
    if re.match(r"^(?:" + _UNIT_ALT + r")", head):
        # `0.03 毫米`、`1.5 倍` 是数值条款，不是条号
        return None
    return dotted.group(1)


def _cells(line: str) -> list[tuple[str, int]]:
    """表格行按单元格切分：数字与单位只允许在同一单元格内绑定。"""
    if "|" not in line:
        return [(line, 0)]
    parts: list[tuple[str, int]] = []
    offset = 0
    for cell in line.split("|"):
        parts.append((cell, offset))
        offset += len(cell) + 1
    return parts


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _key(entry: dict[str, Any]) -> tuple[Any, str]:
    """比对键：数值换算到基本单位后与基本单位配对，措辞与写法差异被抹平。"""
    unit = str(entry.get("unit") or "")
    factor, base = _BASE_UNITS.get(unit, (1.0, unit))
    number = _to_float(entry.get("value"))
    if number is None:
        return (None, base)
    return (round(number * factor, 9), base)


def _context(line: str, start: int, end: int, limit: int = 60) -> str:
    """该数字所在的原句，超长时围绕数字截到 limit 字以内。"""
    left = start
    while left > 0 and line[left - 1] not in _SENTENCE_CUT:
        left -= 1
    right = end
    while right < len(line) and line[right] not in _SENTENCE_CUT:
        right += 1
    while left < start and line[left] in " \t|":
        left += 1
    while right > end and line[right - 1] in " \t|":
        right -= 1
    if right - left <= limit:
        return _clean(line[left:right])
    span = end - start
    pad = max(0, (limit - span) // 2)
    window_left = max(left, start - pad)
    window_right = min(right, window_left + limit)
    window_left = max(left, window_right - limit)
    return _clean(line[window_left:window_right])


def _phrase(line: str, start: int, end: int, limit: int = 28) -> str:
    """数字附近的分句，用于人话说明里的引文。"""
    left = start
    while left > 0 and line[left - 1] not in _PHRASE_CUT:
        left -= 1
    right = end
    while right < len(line) and line[right] not in _PHRASE_CUT:
        right += 1
    while left < start and line[left] in " \t|":
        left += 1
    while right > end and line[right - 1] in " \t|":
        right -= 1
    text = _clean(line[left:right])
    if len(text) > limit:
        text = text[-limit:]
    return text


def _entry(value, unit, line, start, end, lineno, page, section) -> dict[str, Any]:
    # 说明文字取自**原始行**（归一化是 1:1 逐字符的，索引可直接回指原文），
    # 这样引文保留原文的全角标点与数字写法。
    return {
        "value": value,
        "unit": unit,
        "context": _context(line, start, end),
        "para": section or ("L%d" % lineno),
        "line": lineno,
        "page": page,
        "phrase": _phrase(line, start, end),
        "kind": _UNIT_KIND.get(unit, "other"),
    }


def _scan_cell(out, line, cell, offset, lineno, page, section, raw_line="") -> None:
    context_line = raw_line or line
    for match in _TOKEN.finditer(cell):
        unit = _UNIT_CANON.get(match.group("unit"), "")
        if not unit:
            continue
        start = offset + match.start()
        end = offset + match.end()
        ranged = _RANGE_TAIL.search(cell[: match.start()])
        if ranged:
            left_start = offset + ranged.start(1)
            left_end = left_start + len(ranged.group(1))
            out.append(
                _entry(
                    ranged.group(1), unit, context_line, left_start, left_end,
                    lineno, page, section,
                )
            )
        out.append(
            _entry(match.group("num"), unit, context_line, start, end, lineno, page, section)
        )


def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, int]] = set()
    out: list[dict[str, Any]] = []
    for entry in entries:
        token = (str(entry.get("value")), str(entry.get("unit")), int(entry.get("line") or 0))
        if token in seen:
            continue
        seen.add(token)
        out.append(entry)
    return out


def _extract(text: Any) -> list[dict[str, Any]]:
    source = _as_text(text)
    if not source.strip():
        return []
    entries: list[dict[str, Any]] = []
    page: int | None = None
    section = ""
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = _normalize(raw)
        found_page = _page_of(line)
        if found_page is not None:
            page = found_page
        label = _heading_label(line)
        if label:
            section = label
        if not line.strip() or _RULE_LINE.match(line):
            continue
        for cell, offset in _cells(line):
            if not cell.strip():
                continue
            _scan_cell(entries, line, cell, offset, lineno, page, section, raw_line=raw)
    return _dedupe(entries)


# ---------------------------------------------------------------------------
# 成稿侧定位：这个数字本该写在哪一节
# ---------------------------------------------------------------------------

# 这些四字片段是「要求强度词」而不是「对象名词」，出现在任何章节里都不算线索。
_WEAK_ANCHORS = ("不小", "不大", "不低", "不超", "符合", "要求", "规定", "一般", "应按", "原文")


def _sections(text: Any) -> list[dict[str, Any]]:
    source = _as_text(text)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] = {"label": "", "start": 1, "lines": []}
    for index, raw in enumerate(source.splitlines(), 1):
        line = _normalize(raw)
        label = _heading_label(line)
        if label:
            sections.append(current)
            current = {"label": label, "start": index, "lines": [line]}
        else:
            current["lines"].append(line)
    sections.append(current)
    return [
        {"label": item["label"], "start": item["start"], "text": "\n".join(item["lines"])}
        for item in sections
    ]


def _anchors(context: str) -> list[str]:
    runs = re.findall(r"[\u4e00-\u9fff]{2,}", _as_text(context))
    grams: list[str] = []
    for run in runs:
        if len(run) <= 4:
            grams.append(run)
        else:
            grams.extend(run[i: i + 4] for i in range(len(run) - 3))
    return [
        gram
        for gram in dict.fromkeys(grams)
        if not any(word in gram for word in _WEAK_ANCHORS)
    ]


def _hint_label(hint: Any) -> str:
    text = _clean(_normalize(hint))
    if not text:
        return ""
    chapter = re.search(r"第\s*(\d+(?:\.\d+)*)\s*[章节部分]", text)
    if chapter:
        return chapter.group(1)
    dotted = re.match(r"^(\d+(?:\.\d+)*)", text)
    if dotted:
        return dotted.group(1)
    return text


def _locate_section(context: str, sections: list[dict[str, Any]], hint: Any) -> str:
    anchors = _anchors(context)
    best_label, best_score = "", 0
    if anchors:
        for section in sections:
            label = section.get("label") or ""
            if not label:
                continue
            score = sum(1 for anchor in anchors if anchor in section.get("text", ""))
            if score > best_score:
                best_label, best_score = label, score
    return best_label or _hint_label(hint)


def _locate_text(label: str, *, spaced: bool = False) -> str:
    if not label:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)*", label):
        return "第 %s 节" % label if spaced else "第%s节" % label
    return "「%s」" % label


def _display(entry: dict[str, Any]) -> str:
    return "%s%s" % (entry.get("value") or "", entry.get("unit") or "")


def _quote(entry: dict[str, Any]) -> str:
    phrase = str(entry.get("phrase") or "").strip()
    if phrase and len(phrase) <= 28:
        return phrase
    return str(entry.get("context") or "").strip()


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def requirement_numbers(tender_text: Any) -> list[dict[str, Any]]:
    """抽出招标原文里「带单位的数值要求」条目（保留出现位置，不做去重合并）。

    每条：`{value, unit, context, para, line, page, phrase, kind}`。
    `value` 是原文数字串（全角已折成半角），`unit` 是归一单位，
    `context` 是该数字所在原句（≤60 字），`para` 是条号或 `L+行号`。
    页码、章节编号、日期、金额千分位、表格分隔行不会成为条目。
    """
    try:
        return _extract(tender_text)
    except Exception:  # pragma: no cover - 防御性兜底，正常输入不会走到
        return []


def response_numbers(response_text: Any) -> list[dict[str, Any]]:
    """抽出成稿里的数值条目，口径与 `requirement_numbers` 完全一致。"""
    try:
        return _extract(response_text)
    except Exception:  # pragma: no cover
        return []


def missing_numbers(
    tender_text: Any, response_text: Any, *, context_hint: str = ""
) -> list[dict[str, Any]]:
    """原文有、成稿里找不到（数字+单位归一后）的数值，每个数值只报一次。

    返回 `{value, unit, context, para, line, page, section, phrase, kind, suggestion}`。
    """
    try:
        requirements = _extract(tender_text)
        written = _extract(response_text)
    except Exception:  # pragma: no cover
        return []
    if not requirements:
        return []
    present = {_key(entry) for entry in written}
    sections = _sections(response_text)
    seen: set[tuple[Any, str]] = set()
    result: list[dict[str, Any]] = []
    for entry in requirements:
        token = _key(entry)
        if token in seen:
            continue
        seen.add(token)
        if token in present:
            continue
        section = _locate_section(entry.get("context", ""), sections, context_hint)
        where = _locate_text(section) or "成稿"
        quote = _quote(entry)
        suggestion = "%s未写出原文的 %s（%s）" % (where, _display(entry), quote)
        result.append({**entry, "section": section, "suggestion": suggestion})
    return result


def fabricated_numbers(
    response_text: Any, tender_text: Any, *, context_hint: str = ""
) -> list[dict[str, Any]]:
    """反向对账：成稿里有、原文里没有（数字+单位归一后）的数值，用于防幻觉。

    返回结构与 `missing_numbers` 一致，`suggestion` 措辞为「出现原文没有的…」。
    """
    try:
        written = _extract(response_text)
        requirements = _extract(tender_text)
    except Exception:  # pragma: no cover
        return []
    if not written:
        return []
    known = {_key(entry) for entry in requirements}
    sections = _sections(response_text)
    seen: set[tuple[Any, str]] = set()
    result: list[dict[str, Any]] = []
    for entry in written:
        token = _key(entry)
        if token in seen:
            continue
        seen.add(token)
        if token in known:
            continue
        section = _locate_section(entry.get("context", ""), sections, context_hint)
        where = _locate_text(section) or "成稿"
        quote = _quote(entry)
        suggestion = "%s出现原文没有的 %s（%s）" % (where, _display(entry), quote)
        result.append({**entry, "section": section, "suggestion": suggestion})
    return result


def fidelity_gaps(
    tender_text: Any, response_text: Any, *, context_hint: str = ""
) -> list[str]:
    """给生成/导出侧用的人话说明：中性定位，不判合规。

    形如 `「第 4.3.8 节」缺少原文第 32 页的「沿齿长方向不小于60%」`；
    成稿里定位不到章节时退化为 `成稿缺少原文的「…」`。
    """
    gaps: list[str] = []
    try:
        missing = missing_numbers(tender_text, response_text, context_hint=context_hint)
    except Exception:  # pragma: no cover
        return gaps
    for entry in missing:
        section = str(entry.get("section") or "")
        if not section:
            location = "成稿"
        else:
            located = _locate_text(section, spaced=True)
            location = located if located.startswith("「") else "「%s」" % located
        page = entry.get("page")
        origin = "原文第 %s 页的" % page if page else "原文的"
        gaps.append("%s缺少%s「%s」" % (location, origin, _quote(entry)))
    return gaps