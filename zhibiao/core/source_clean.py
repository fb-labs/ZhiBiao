"""招标原文清洗：剔除 PDF 每页重复的页眉/页脚，避免它们污染标的判定。

真实事故（2026-10-19，项目「新3」减速器中小修理）：
采购代理把上一版「立柱、千斤等修理」的响应文件格式章整段复制进本项目的采购文件，
却没改页眉。于是提取文本里

    山西新元煤炭有限责任公司立柱、千斤等修理询比采购文件

重复出现 **10 次**（正是「第六章 响应文件格式」那 10 页）。后果是
``classify_subject`` 的证据词里混进了「立柱」，subtype 从「通用装备类」
被推到「煤矿液压支架类」，大纲随即被 ``REWRITE_SUPPORT`` 换成液压支架专属骨架
（总体技术方案与架型选择／电液控制系统与智能化功能／支架压力监测与数据上传／
井下支护安全与顶板管理措施）——一份减速器标书里长出了液压支架的章节。

清洗只针对两类行，并带若干硬保护，宁可漏删不可误删：

* 只删长度 6~60 的行，整段正文不会被删；
* 含句末标点（。！？；：）的行一律保留 —— 页眉页脚不会是可读句子；
* 同一行重复不足 3 次不删；
* 页码行（``第 39页 …编制``）单独识别：它逐页变化，精确匹配抓不到；
* 删除字符数超过全文 40% 时整体放弃并回报原因。
"""
# ── 规则来源约定（用户 2026-10-19 明确要求）──────────────────────────────────
# 上面那段是**示例项目**的事故记录，说明这条护栏为什么存在，**不是规则本身**。
# 本项目上传件都是例子，任何一轮改动都不得把示例里的公司名/矿名/项目名/章节名
# 变成生成逻辑。本模块只允许依赖：
#   1) 通用文本规律（每页重复的短行、页码行、长度与标点分布…）；
#   2) 运行时从当前项目的招标原文里算出来的数据。
# 严禁为某个示例项目写特判；回归见 tests/test_no_example_data_in_logic.py。
from __future__ import annotations

import re
from typing import Any

PAGE_LINE = re.compile(r"^第\s*\d+\s*页")
# 页脚常见写法： 第 39页 山西华普项目管理有限公司编制
PAGE_LINE_LOOSE = re.compile(r"^第\s*\d{1,4}\s*页\s*[^\n]{0,60}$")

SENTENCE_MARKS = ("。", "！", "？", "；", "：", "!", "?", ";", ":")
HEADER_MIN_LEN = 6
HEADER_MAX_LEN = 60
HEADER_MIN_REPEATS = 3
MAX_REMOVAL_RATIO = 0.4
# 页眉页脚会**几乎每页**出现；表格里重复的单元格（「（满分 5 分）」这种）只会出现几次。
# 只按「重复 >= 3 次」判断，会把三处同分的评分项一起删掉——实测 100 分表被削成 85 分。
# 所以要求重复次数至少占估算页数的一半。
PAGE_ESTIMATE_CHARS = 1800
# 明显的表格/分值内容，一律不当页眉
_SCORE_CELL = re.compile(r"(满分|分值|得分|扣分|加分)\s*[:：]?\s*[-+]?\d|\d\s*分\s*[）)]|[（(][^）)]{0,10}分[）)]")
_STRUCTURE_PREFIXES = ("#", "|", "```", ">", "- ", "* ")


def _clean_line(raw: Any) -> str:
    """归一化空白（全角空格也算），用于判断「是否同一行」。"""
    text = str(raw if raw is not None else "").replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def looks_like_running_header(line: str) -> bool:
    """这一行像不像页眉/页脚：短、重复、且不像可读句子或结构行。"""
    if not (HEADER_MIN_LEN <= len(line) <= HEADER_MAX_LEN):
        return False
    if any(mark in line for mark in SENTENCE_MARKS):
        return False
    if line.startswith(_STRUCTURE_PREFIXES):
        return False
    # 至少要有 4 个实义字符，避免把「合计」「备注」这类表头碎片或纯符号行当成页眉
    return sum(1 for ch in line if ch.isalnum() or "\u4e00" <= ch <= "\u9fff") >= 4


def detect_running_headers(text: Any) -> list[str]:
    """找出重复 >= 3 次的短行（含逐页变化的页码行模式）。长行优先，便于整体剔除。"""
    body = str(text if text is not None else "")
    counts: dict[str, int] = {}
    for line in body.splitlines():
        key = _clean_line(line)
        if key and looks_like_running_header(key):
            counts[key] = counts.get(key, 0) + 1
    # 页数：优先数「第 N 页」页脚，没有就按字数估（一份标书一页 1500~2000 字）
    pages = len(PAGE_LINE.findall(body)) or max(1, len(body) // PAGE_ESTIMATE_CHARS)
    threshold = max(HEADER_MIN_REPEATS, int(round(pages * 0.5)))
    repeated = {key for key, times in counts.items()
                if times >= threshold and not _SCORE_CELL.search(key)}
    pages_rows = {key for key in counts if PAGE_LINE.search(key)}
    return sorted(repeated | pages_rows, key=len, reverse=True)


def strip_running_headers(text: Any) -> tuple[str, dict[str, Any]]:
    """清洗招标原文，返回 (清洗后文本, 报告)。

    报告字段：``removed``（被剔除的页眉/页脚原文）、``removed_lines``、``removed_chars``、
    ``skipped``（放弃清洗的原因，None 表示已清洗）。
    """
    original = str(text if text is not None else "")
    if not original.strip():
        return original, {"removed": [], "removed_lines": 0, "removed_chars": 0, "skipped": "empty"}

    headers = detect_running_headers(original)
    if not headers:
        return original, {"removed": [], "removed_lines": 0, "removed_chars": 0, "skipped": "no_repeats"}

    kept_lines: list[str] = []
    removed_lines = 0
    for line in original.splitlines():
        key = _clean_line(line)
        if key and (key in headers or PAGE_LINE.search(key)):
            removed_lines += 1
            continue
        # 页眉可能被抽取器和正文挤在同一行：把已证实的页眉整串扣掉，保留其余内容。
        stripped = line
        for header in headers:
            if header in stripped and header != _clean_line(stripped):
                stripped = stripped.replace(header, "")
        kept_lines.append(stripped)
    cleaned = "\n".join(kept_lines)

    removed_chars = len(original) - len(cleaned)
    if removed_chars > len(original) * MAX_REMOVAL_RATIO:
        # 安全阀：删除量异常大说明规则抓错了东西（例如整本都是短行表格），宁可不清洗。
        return original, {"removed": headers, "removed_lines": 0, "removed_chars": 0,
                          "skipped": "removal_too_large"}

    return cleaned, {"removed": headers, "removed_lines": removed_lines,
                     "removed_chars": removed_chars, "skipped": None}


def clean_tender_text(text: Any) -> str:
    """读取招标原文时的统一入口：只需要清洗后文本的调用点用这个。"""
    return strip_running_headers(text)[0]