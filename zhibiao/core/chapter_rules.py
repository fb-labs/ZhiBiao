"""Outline-aware rules for safe, predictable bid-body generation."""

from __future__ import annotations

import re
from typing import Any

from zhibiao.core.image_slots import image_placeholder


MIN_CHAPTER_WORDS = 120
MAX_CHAPTER_WORDS = 5000
OVERVIEW_WORDS = 220
DEFAULT_LEAF_WORDS = 1200
LENGTH_FIXED = 53.0
LENGTH_SLOPE = 1.348
# 章节层级按 relative depth 编号，段数 = 深度；脏数据里出现 level=999 时不能生成上千段的编号串。
# 线上真实目录 level ∈ {1,2}，导出侧也把标题层级夹在 1..4，所以 6 层上限不可能影响真实数据。
MAX_LEVEL_DEPTH = 6

_MATERIAL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    # 「法定代表人身份证明」「授权委托书」不在这里：它们是投标人自己出具、按格式填写的
    # 文书，交给 app/core/legal_forms.py 出稿。以前塞进材料占位，整章只剩
    # 「本节用于提交…扫描件」一句话，正文等于没写（用户实测报错即此处）。
    (("营业执照",), "有效营业执照扫描件"),
    (("ISO", "体系认证证书", "质量管理体系认证", "环境管理体系认证"), "有效期内的体系认证证书扫描件"),
    (("产品合格证", "合格证明", "检测报告", "检验报告"), "对应产品的合格证或检测报告扫描件"),
    (("获奖证明", "奖项证明", "荣誉证书"), "获奖证明材料扫描件"),
    (("财务报表", "审计报告", "财务报告"), "招标文件要求年度的财务报表或审计报告"),
    (("资格证明", "资格文件", "资质证书"), "招标文件要求的资格或资质证明材料扫描件"),
)

# 「标题本身就是材料名」才整章换成材料插入占位：带动作/说明类名词的标题是要写的正文。
# 实测旧实现把「财务报表分析」「检测报告汇总与解读」整章替换成占位并跳过质量校验。
_MATERIAL_ACTION_WORDS = ("分析", "编制", "汇总", "解读", "要点", "方案", "流程", "管理", "说明", "概述", "运行", "保持")
# 长标题只接受可完整解释为材料提交要求的外围词，不因长度直接放开任意说明章。
_MATERIAL_QUALIFIERS_RE = re.compile(
    r"(?:投标人|有效期内|有效|的|复印件|扫描件|副本|正本|证书|加盖单位公章|加盖公章)*"
)
_MATERIAL_TITLE_MAX_LEN = 12
_MATERIAL_TITLE_SLACK = 4

# 正文内嵌小标题（二级及以下）。一级 # 是章节标题本身，正文里不该出现，一律不动。
SUBHEADING_RE = re.compile(r"^(#{2,6})[ \t]*(.*?)[ \t]*$")
_HEADING_MEASUREMENT_RE = re.compile(r"^\s*\d+(?:[.．]\d+)?\s*(?:MPa|kPa|kN|kW|mm|cm|kg|m|%|％)(?![A-Za-z])", re.I)
_CODE_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")
_CN_NUM = "一二三四五六七八九十百零两壹贰叁肆伍陆柒捌玖拾"
# 行首编号前缀（宽松版，用于剥除）：多级阿拉伯号 / 括号号 / 顿号号 / 中文数字号 / 第X节。
# 分隔符必须紧跟数字（不接受「1 .」），否则重复执行会把正文里的「、」吃掉，破坏幂等。
_NUMBER_PREFIX_RE = re.compile(
    r"^[ \t\u3000]*(?:"
    r"[（(][ \t]*(?:\d+(?:[.．]\d+)*|[%s]+)[ \t]*[)）]"
    r"|第[ \t]*\d+[ \t]*[章节部分条款项]"
    r"|第[ \t]*[%s]+[章节部分条款项]"
    r"|第[ \t]*(?:\d+|[%s]+)(?=[ \t.．、:：)）]|$)"
    r"|\d+(?:[.．]\d+)*[.．、)）:：]?"
    r"|[%s]+[、.．)）:：]"
    r")[ \t\u3000]*" % ((_CN_NUM,) * 4)
)
# 质量门禁用的严格版：裸数字+空格（「3 种工况」）不算编号，避免误报。
SUBHEADING_NUMBER_RE = re.compile(
    r"^[ \t\u3000]*(?:"
    r"[（(][ \t]*(?:\d+(?:[.．]\d+)*|[%s]+)[ \t]*[)）]"
    r"|第[ \t]*\d+[ \t]*[章节部分条款项]"
    r"|第[ \t]*[%s]+[章节部分条款项]"
    r"|第[ \t]*(?:\d+|[%s]+)(?=[ \t.．、:：)）]|$)"
    r"|\d+(?:[.．]\d+)+"
    r"|\d+[.．、)）:：]"
    r"|[%s]+[、.．)）:：]"
    r")" % ((_CN_NUM,) * 4)
)


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9}
_CN_HEAD = re.compile(r"^\s*([一二三四五六七八九十]{1,3})、")


def _cn_number(text: str) -> int | None:
    """把「十二、」这种中文序号转成 12；没有序号返回 None。"""
    match = _CN_HEAD.match(str(text or ""))
    if not match:
        return None
    raw = match.group(1)
    if raw == "十":
        return 10
    if raw.startswith("十"):
        return 10 + _CN_DIGITS.get(raw[1:], 0)
    if "十" in raw:
        head, _, tail = raw.partition("十")
        return _CN_DIGITS.get(head, 0) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    return _CN_DIGITS.get(raw)


def section_numbers(rows: list[dict[str, Any]]) -> dict[str, str]:
    """按 level 与出现顺序算章节号：1、1.1、1.1.1。

    为什么不用全局 seq：正文写作时模型会把传入的 seq 当作章节号写进标题，
    于是导出后出现「2 → 4.1 → 6.1 → 11.1 → 16.1 → 34.1 → 42.1」的跳号（实测）。

    层级按「相对深度」归一：level=1 是第 1 层，跳级时补齐中间层（缺失层计数从 1 开始），
    因此编号段数始终等于该行的相对深度，同一份大纲不会出现两个相同章节号。
    线上真实目录 level ∈ {1,2} 交替出现，编号结果与旧实现逐字一致（回归用例锁定）。
    """
    counters: list[int] = []
    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        depth = min(MAX_LEVEL_DEPTH, max(1, int(row.get("level") or 1)))
        # 招标原件的章节自带序号（「一、」「二、」「十二、」）。原件的「二、」出现两次
        # （法定代表人身份证明 / 授权委托书 二选一），按顺序计数器会一路错开，补章就变成
        # 「十三、」底下的 13.1~13.6，和正文的「十二、」对不上。有自带序号就以它为准，
        # 子章节跟着父章节的数字走（十二 → 12.1~12.6）。
        if depth == 1:
            named = _cn_number(row.get("title"))
            if named is not None:
                counters = [named]
                key = str(row.get("id") or "")
                if key and key not in out:
                    out[key] = str(named)
                continue
        if len(counters) < depth:
            counters.extend([1] * (depth - len(counters)))
        else:
            del counters[depth:]
            counters[depth - 1] += 1
        key = str(row.get("id") or "")
        # id 为空或重复时只保留第一次写入的值：后一行不能覆盖已确定的章节号。
        if key and key not in out:
            out[key] = ".".join(str(counter) for counter in counters)
    return out


def shape_chapters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return flat chapter records enriched with parent path and child scope."""
    shaped: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        item["id"] = str(item.get("id") or "")
        item["seq"] = int(item.get("seq") or 0)
        item["level"] = max(1, int(item.get("level") or 1))
        item["title"] = str(item.get("title") or "").strip()
        item["children"] = []
        while stack and stack[-1]["level"] >= item["level"]:
            stack.pop()
        parent = stack[-1] if stack else None
        if parent is not None:
            parent["children"].append(item["title"])
        item["path"] = " > ".join(
            [str(node["title"]) for node in stack] + [item["title"]]
        )
        item["parent_title"] = str(parent["title"]) if parent else ""
        item["parent_id"] = str(parent["id"]) if parent else ""
        item["has_children"] = False
        if parent is not None:
            parent["has_children"] = True
        shaped.append(item)
        stack.append(item)
    return shaped


def estimate_word_budgets(
    chapters: list[dict[str, Any]],
    total_words: int,
    *,
    overview_words: int = OVERVIEW_WORDS,
    fixed: float = LENGTH_FIXED,
    slope: float = LENGTH_SLOPE,
) -> dict[str, int]:
    """Reverse-estimate leaf prompt budgets while reserving branch summaries."""
    branches = [c for c in chapters if c.get("has_children")]
    leaves = [c for c in chapters if not c.get("has_children")]
    budgets: dict[str, int] = {}
    for chapter in branches:
        explicit = int(chapter.get("target_words") or 0)
        budgets[chapter["id"]] = max(
            MIN_CHAPTER_WORDS,
            min(800, explicit if explicit > 0 else overview_words),
        )

    if total_words <= 0 or not leaves:
        # 调用方把 length<=0 解读为「篇幅不限，按本节实际任务展开」，所以这里绝不能给 0：
        # 总量未知时没有显式目标的叶子按 DEFAULT_LEAF_WORDS 展开，而不是比有上限的章节还宽松。
        for chapter in leaves:
            explicit = int(chapter.get("target_words") or 0)
            budgets[chapter["id"]] = (
                max(MIN_CHAPTER_WORDS, min(MAX_CHAPTER_WORDS, explicit))
                if explicit > 0 else DEFAULT_LEAF_WORDS
            )
        return budgets

    branch_overhead = sum(fixed + slope * budgets[c["id"]] for c in branches)
    fixed_leaf_actual = 0.0
    unassigned: list[dict[str, Any]] = []
    for chapter in leaves:
        explicit = int(chapter.get("target_words") or 0)
        if explicit > 0:
            budget = max(MIN_CHAPTER_WORDS, min(MAX_CHAPTER_WORDS, explicit))
            budgets[chapter["id"]] = budget
            fixed_leaf_actual += fixed + slope * budget
        else:
            unassigned.append(chapter)

    if unassigned:
        remaining = max(0.0, float(total_words) - branch_overhead - fixed_leaf_actual)
        per_leaf = round((remaining / len(unassigned) - fixed) / max(0.1, slope))
        per_leaf = max(MIN_CHAPTER_WORDS, min(MAX_CHAPTER_WORDS, per_leaf))
        for chapter in unassigned:
            budgets[chapter["id"]] = per_leaf
    return budgets


def _cjk_only(text: str) -> str:
    """去掉字母、数字、空白和标点，只留中文，用于比对材料名本体。"""
    return re.sub(r"[^\u4e00-\u9fff]", "", str(text or ""))


def _material_term(clean: str, terms: tuple[str, ...]) -> str:
    """标题命中的最长材料名；「ISO9001 体系认证证书」这类带编号的写法按中文骨架比对。"""
    folded = clean.casefold()
    skeleton = _cjk_only(clean)
    matched = ""
    for term in terms:
        term_skeleton = _cjk_only(term)
        if (term.casefold() in folded) or (term_skeleton and term_skeleton in skeleton):
            if len(term) > len(matched):
                matched = term
    return matched


def material_insert_placeholder(title: str) -> str | None:
    """Material-only chapters are handled with explicit insertion placeholders.

    只有「标题本身就是材料名及明确提交要求」才走占位。「管理」属于质量管理体系认证
    材料名时不是动作；材料名之外的 分析/汇总/解读/运行/保持 等仍须生成正文。
    长标题只为有效/复印件/盖章等有限材料修饰词放行，不能整章误换成占位。
    """
    clean = str(title or "").strip()
    skeleton = _cjk_only(clean)
    for terms, label in _MATERIAL_RULES:
        term = _material_term(clean, terms)
        if not term:
            continue
        term_skeleton = _cjk_only(term)
        remainder = skeleton.replace(term_skeleton, "", 1) if term_skeleton else skeleton
        # 只豁免材料本体里的动作词（如「质量管理体系认证」的「管理」），
        # 外围「运行保持」「管理方案」等任务仍不能整章换成材料占位。
        if any(word in remainder for word in _MATERIAL_ACTION_WORDS):
            continue
        if (len(clean) > _MATERIAL_TITLE_MAX_LEN
                and len(skeleton) > len(term_skeleton) + _MATERIAL_TITLE_SLACK
                and not _MATERIAL_QUALIFIERS_RE.fullmatch(remainder)):
            continue
        return (
            f"本节用于提交{label}。请按招标文件要求提供真实、有效且加盖所需印章的材料；"
            "系统不会编造证书编号、人员身份或业绩信息。\n\n"
            f"{{{{待填:{label}}}}}\n\n"
            # 材料章的核心内容就是那张扫描件：明确给出预留图框，
            # 说明文字由 image_slots 统一生成（插什么/要不要盖章/要不要截全）。
            + image_placeholder(label)
        )
    return None


def _fact_pair(item: Any) -> tuple[str, str] | None:
    """facts 由上游组装，偶有非二元组脏数据；这里只过滤，不改动正常项。"""
    if isinstance(item, (list, tuple)) and len(item) == 2:
        key, value = str(item[0] or "").strip(), str(item[1] or "").strip()
        if key and value:
            return key, value
    return None


def safe_structured_draft(
    title: str,
    facts: list[tuple[str, str]],
    scores: list[dict[str, Any]],
) -> str:
    """Create a visibly incomplete, fact-safe scaffold when no model is configured."""
    name = str(title or "本节").strip()
    pairs = [pair for pair in (_fact_pair(item) for item in (facts or [])) if pair][:8]
    fact_lines = "\n".join(f"- {key}：{value}" for key, value in pairs) or \
        "- 招标文件对应条款：{{待填:本节相关条款原文}}"
    score_lines = "\n".join(
        f"- {x.get('name')}（{x.get('score') or ''}分）：{{{{待填:按评分标准补充响应内容}}}}"
        for x in (scores or [])[:3] if isinstance(x, dict) and x.get("name")
    )
    sections = [
        f"## {name}",
        "### 响应依据\n{{待填:招标文件对应条款及原文要求}}",
        "### 具体响应\n{{待填:结合本项目真实方案、流程、责任岗位和验收方式补充}}",
    ]
    if score_lines:
        sections.append("### 评分项响应\n" + score_lines)
    sections.append(
        "### 项目事实\n" + fact_lines
        + "\n\n以上信息仅作已知事实索引；未提供的企业资质、人员、业绩、证书编号、价格和专利信息，"
        "请使用 {{待填:具体信息}} 占位，不得补造。"
    )
    return "\n\n".join(sections)


def heading_number(text: str) -> tuple[str, str]:
    """识别内嵌标题文字行首的编号，返回 ("arabic" | "cjk" | "", 编号本体)。

    只认行首编号：「3 种工况」「ISO 9001」这类出现在标题文字里的数字不是编号，不得误判。
    """
    body = str(text or "")
    if _HEADING_MEASUREMENT_RE.match(body):
        return "", ""
    if not SUBHEADING_NUMBER_RE.match(body):
        return "", ""
    matched = _NUMBER_PREFIX_RE.match(body)
    raw = matched.group(0).strip() if matched else ""
    if not raw:
        return "", ""
    if raw.startswith("第") or re.search(r"[%s]" % _CN_NUM, raw):
        return "cjk", raw
    digits = re.match(r"[（(]?[ \t]*(\d+(?:[.．]\d+)*)", raw)
    if not digits:
        return "", ""
    return "arabic", digits.group(1).replace("．", ".")


def _split_line_endings(text: str) -> list[list[str]]:
    """切成 [正文, 行尾] 列表，保持原有行尾与空行，不增删行。"""
    rows: list[list[str]] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            rows.append([line[:-2], "\r\n"])
        elif line.endswith("\n"):
            rows.append([line[:-1], "\n"])
        elif line.endswith("\r"):
            rows.append([line[:-1], "\r"])
        else:
            rows.append([line, ""])
    return rows


def _renumber_rows(rows: list[list[str]], number: str) -> int:
    """就地重编号 [正文, 行尾] 行表，返回改动条数。"""
    # 代码围栏只有成对出现时才可信；未闭合的围栏不能把后半章都吃掉，此时按普通行处理。
    track_fence = sum(1 for body, _ending in rows if _CODE_FENCE_RE.match(body)) % 2 == 0
    in_fence = False
    heads: list[tuple[int, str, str]] = []
    for index, row in enumerate(rows):
        body = row[0]
        if track_fence and _CODE_FENCE_RE.match(body):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = SUBHEADING_RE.match(body)
        if matched:
            heads.append((index, matched.group(1), matched.group(2)))
    if not heads:
        return 0
    # 本章正文最浅的标题层级算第 1 层，深层出现时重置更深计数。
    base = min(len(hashes) for _index, hashes, _body in heads)
    counters: list[int] = []
    changed = 0
    for index, hashes, body_text in heads:
        depth = max(1, len(hashes) - base + 1)
        if len(counters) < depth:
            counters.extend([1] * (depth - len(counters)))
        else:
            del counters[depth:]
            counters[depth - 1] += 1
        title = (body_text if _HEADING_MEASUREMENT_RE.match(body_text) else _NUMBER_PREFIX_RE.sub("", body_text, count=1)).strip()
        head = ".".join([number] + [str(counter) for counter in counters])
        new_body = f"{hashes} {head}" + (f" {title}" if title else "")
        if new_body != rows[index][0]:
            rows[index][0] = new_body
            changed += 1
    return changed


def normalize_inline_numbering(content: str, number: str) -> tuple[str, int]:
    """把正文里的内嵌小标题编号统一改成「本章节号.序号」体系，返回 (新正文, 改动条数)。

    导出侧给章节标题按大纲统一定号（1 / 1.1 / 1.1.1），正文里的 ## 小标题必须落在同一套
    体系里，否则 Word 里会出现「2.2 总体结构与主要技术参数」下面又冒出「1.1 结构组成」
    这类与目录撞号、重复编号的标题（实测）。

    只处理 Markdown 标题行（``^#{2,6}``）：一级 # 不动；表格行、有序/无序列表、代码块内的
    # 注释、正文段落一律不碰。已经规范的文本再跑一次返回 (原文, 0)；number 为空字符串时
    原样返回 (content, 0)。
    """
    text = str(content or "")
    label = str(number or "").strip()
    if not text or not label:
        return text, 0
    rows = _split_line_endings(text)
    changed = _renumber_rows(rows, label)
    if not changed:
        return text, 0
    return "".join(body + ending for body, ending in rows), changed


def container_chapter_numbers(numbers: Any) -> set[str]:
    """挑出「容器章」：自身还有下级章节（编号是别的章节号前缀）的章节号。

    这类章节正文是总述，内嵌小标题若也按「本章号.序号」编号，就会与下级章节号撞号
    （实测第 2 章「技术方案总体设计」里的 `## 2.1 本章范围与总体原则` 与真正的 2.1 章
    「总体技术方案与架型选择」同名，Word 目录里出现两个 2.1），所以容器章内嵌标题不编号。
    """
    labels = {str(item or "").strip() for item in (numbers or []) if str(item or "").strip()}
    return {label for label in labels if any(other.startswith(label + ".") for other in labels)}


def strip_inline_numbering(content: str) -> tuple[str, int]:
    """去掉内嵌小标题行首的编号（阿拉伯号与汉字序号），返回 (新正文, 改动条数)。

    与 normalize_inline_numbering 同一套边界：只处理 ``^#{2,6}`` 行，一级标题、表格行、
    有序/无序列表、正文段落、代码块一律不碰；行数与行尾保持不变；无可剥编号返回 (原文, 0)。
    """
    text = str(content or "")
    if not text:
        return text, 0
    rows = _split_line_endings(text)
    # 围栏只有成对出现才可信（与 _renumber_rows 一致），否则未闭合围栏会吃掉后半章
    track_fence = sum(1 for body, _ending in rows if _CODE_FENCE_RE.match(body)) % 2 == 0
    in_fence = False
    changed = 0
    for row in rows:
        body = row[0]
        if track_fence and _CODE_FENCE_RE.match(body):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = SUBHEADING_RE.match(body)
        if not matched:
            continue
        if _HEADING_MEASUREMENT_RE.match(matched.group(2)):
            continue
        title = _NUMBER_PREFIX_RE.sub("", matched.group(2), count=1).strip()
        new_body = matched.group(1) + ((" " + title) if title else "")
        if new_body != body:
            row[0] = new_body
            changed += 1
    if not changed:
        return text, 0
    return "".join(body + ending for body, ending in rows), changed


def renumber_outline_headings(lines_or_text: Any, number: str) -> Any:
    """便捷包装：整段文本或「按行列表」都按同一套章节号重编号，并保持入参形状。"""
    if isinstance(lines_or_text, (list, tuple)):
        rows = [[str(line), ""] for line in lines_or_text]
        label = str(number or "").strip()
        if label:
            _renumber_rows(rows, label)
        return [body for body, _ending in rows]
    return normalize_inline_numbering(str(lines_or_text or ""), number)[0]
