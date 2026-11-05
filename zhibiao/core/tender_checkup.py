"""招标文件体检：生成正文之前，把「解析到底抽到了什么」摊开给人看。

设计原则（这几条决定了它对新招标文件是否仍然有用）：

1. 只用通用结构，不认具体设备名。条目、边界条款、参数块、数量口径都从原文形状
   （编号条目、`由X方自备`、`参数名：值` 行、`N 台/N 套`）推断，不绑定皮带机或支架。
2. 只报告「可核对的抽取结果」，不做结论。体检分低不等于标书会写错，只说明
   **按现在的抽取能力，这份文件里有东西没被抓住**，需要人工补或先改解析。
3. 风险项一律给出「原文依据 + 生成的后果」，方便判断是真问题还是体检误报。
4. **按证据扣分**（不再是「没抽到就扣」）：每个考核项先归入三类 ——
   `extractable`（原文确实给了，抽不到才扣）、`not_in_source`（原文本来没有，
   只显示「原文未提供」）、`note`（原文事实，只提示）。判定与打分拆成两层：
   本模块判「原文有没有」（证据见 `source_evidence` / `_tender_no_line` /
   `_format_chapter_line`），`app.core.checkup_score` 负责算分（权重表在那边，
   分母只含可抽取项）。**没有证据就不许扣分**，这是整条链最不能让步的一条。
"""
from __future__ import annotations

import re
from typing import Any

from zhibiao.core.checkup_score import (
    CHECK_WEIGHTS,
    KIND_EXTRACTABLE,
    KIND_NOTE,
    KIND_NOT_IN_SOURCE,
    score_checkup,
)
from zhibiao.core.completeness import (
    boundary_clauses,
    component_blocks,
    machine_count,
    source_evidence,
    tech_items,
)
from zhibiao.core.tender_parse import (
    FIELD_LABELS,
    clean_value,
    extract_tender_no,
    looks_like_tender_no,
    value_usable,
)

# 封面/前置要素：名称 → (展示名, 是否必须)
COVER_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("项目名称", "招标项目名称", True),
    ("招标编号", "招标编号", True),
    ("项目编号", "项目编号", False),
    ("招标人", "招标人", True),
    ("招标代理机构", "招标代理机构", False),
)

_EXTERNAL_REF = re.compile(r"详见[^。\n]{0,12}(?:技术协议|技术规范书|技术规格书|技术要求附件)"
                           r"|另附[^。\n]{0,8}(?:技术协议|技术规范书)|以技术协议为准")
_PACKAGE = re.compile(r"第[一二三四五六七八九十\d]{1,3}标段|_{0,2}00\d[ \t]*第[一二三四五六七八九十]标段")
_FILL_BRACKET = re.compile(r"【\s*(?:[一二三四五六七八九十百千两\d]{1,4}|[×xX＊*]{2,4}|[ 　]{0,3})\s*】")
# 文本化表格：`a | b | c` 或 用 2 个以上空格分列的行
_PIPE_ROW = re.compile(r"^[^\n|]{0,40}\|[^\n|]{0,40}\|[^\n]*$", re.M)

# 「2 个以上空格分列」的行 = 文本化表格。
#
# 这里**不能用正则**。等价的写法是
#   ^[^\n]{0,30}(?:[ \t]{2,}[^\s][^\n]{0,30}){2,}$
# 但它是灾难性回溯的经典形状：外层 `(?:...){2,}` 无界重复，内层 `[^\n]{0,30}`
# **也能吃空格**，于是「这段空格算分隔符还是算上一格的尾巴」有无穷多种切法；
# 一旦行尾 `$` 不满足（该行不是以「够格的分列单元」收尾），引擎就把所有切法试完，
# 耗时随行长指数增长（实测 96 字 6.8 秒、每多 2 字 ×4）。
# 线上真实后果：招标文件里一句「我单位　　　　（制造商名称）是按…」的 272 字表格行
# 让 `_checkup_facts` 永不返回 → `GET /api/projects/{id}/tender-checkup`、
# `/tender-digest` 卡死 → 单进程 uvicorn 的事件循环被这条纯 C 层循环占满
# （主线程 STAT=R、CPU 100%、access log 停、连容器内 127.0.0.1:8001 都超时）。
#
# 改法：按「2 个以上空格且后面紧跟非空白字符」这个**唯一确定**的边界线性切分
# （见 `_is_space_col_row`），再做与正则完全相同的三条长度校验。数学上等价：
# 分隔符必须是整段空格的末尾（`[^\s]` 不能匹配空格），所以切法是唯一的，不存在歧义。
_SPACE_COL_MAX_HEAD = 30      # 首列：`[^\n]{0,30}`
_SPACE_COL_MAX_CELL = 31      # 每个分列单元：`[^\s]` + `[^\n]{0,30}`
_SPACE_COL_MIN_CELLS = 2      # `{2,}`
_SPACE_COL_RUN = re.compile(r"[ \t]{2,}")
_SPACE_COL_NON_SPACE = re.compile(r"[^\s]")


class _LineMatch:
    """`re.Match` 的最小替身：调用点只用 `.group(0)`。"""

    __slots__ = ("_line",)

    def __init__(self, line: str) -> None:
        self._line = line

    def group(self, index: int = 0) -> str:
        return self._line if index == 0 else ""

    def start(self) -> int:
        return 0

    def end(self) -> int:
        return len(self._line)


class _LinePattern:
    """逐行判定器，接口与 `re.Pattern` 的 `.search` / `.findall` 同形。

    语义等价于 `re.compile(r"^…$", re.M)`：`^`/`$` 把匹配限制在单行内，
    所以对 `text.split("\\n")` 的每一行做整行判定即可（`\\r` 仍留在行尾，
    与 `[^\\n]` 能匹配 `\\r` 的行为一致）。
    """

    def __init__(self, predicate: "Any") -> None:
        self._predicate = predicate

    def search(self, text: Any) -> _LineMatch | None:
        for line in str(text or "").split("\n"):
            if self._predicate(line):
                return _LineMatch(line)
        return None

    def findall(self, text: Any) -> list[str]:
        return [line for line in str(text or "").split("\n") if self._predicate(line)]

    def match(self, line: str) -> _LineMatch | None:
        return _LineMatch(str(line or "")) if self._predicate(str(line or "")) else None


def _is_space_col_row(line: str) -> bool:
    """这一行是不是「用 2 个以上空格分列」的文本化表格行（线性判定）。

    等价于 `^[^\\n]{0,30}(?:[ \\t]{2,}[^\\s][^\\n]{0,30}){2,}$`，但不回溯：

    1. 分隔符只能出现在**一段空格的末尾**——因为分隔符后面必须紧跟 `[^\\s]`，
       而空格本身是 `\\s`，所以 `[ \\t]{2,}` 只能把整段空格吃满。切法唯一。
    2. 于是逐段切分：首列长度 ≤ 30；每个分列单元 1…31 字；分列单元 ≥ 2 个。
    """
    size = len(line)
    # 最短可能的命中：首列 0 字 + 两个「2 空格 + 1 字」单元 = 6 字
    if size < 6:
        return False
    # 线性预筛：整行连一段 2 连空格都没有，不可能命中
    if not _SPACE_COL_RUN.search(line):
        return False
    cells = 0
    cell_start = 0
    pos = 0
    while True:
        found = _SPACE_COL_RUN.search(line, pos)
        if found is None:
            break
        # 后面不是非空白字符 → 这段空格属于上一格（或行尾），不是分隔符
        if found.end() >= size or not _SPACE_COL_NON_SPACE.match(line[found.end()]):
            pos = found.end()
            continue
        if cells == 0:
            if found.start() > _SPACE_COL_MAX_HEAD:
                return False
        elif not 1 <= found.start() - cell_start <= _SPACE_COL_MAX_CELL:
            return False
        cells += 1
        cell_start = found.end()
        pos = found.end()
    if cells < _SPACE_COL_MIN_CELLS:
        return False
    # 最后一格：从最后一个分隔符之后到行尾
    return 1 <= size - cell_start <= _SPACE_COL_MAX_CELL


_SPACE_COL_ROW = _LinePattern(_is_space_col_row)
# 「驱动方式」「结构要求」这类名字后面的参数块不是部件，展示时要标出来，别让人误以为是设备。
_NOT_COMPONENT = re.compile(r"(?:方式|要求|结构|设计|参数|指标|措施|方案|标准|说明)$")


def _risk(level: str, title: str, detail: str, hint: str) -> dict[str, str]:
    return {"level": level, "title": title, "detail": detail, "hint": hint}


_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 「第N章」标题行。**不写死章号**：不同招标文件里投标文件格式可能在第六章、第七章，
# 也可能根本没有（格式直接附在附件里）。一律按标题语义找，找不到就如实说找不到。
_CHAPTER_LINE = re.compile(r"^[ \t]*第([一二三四五六七八九十百零〇\d]{1,3})[ \t]*章[ \t　]*([^\n]{0,50})$")
_DOT_LEADER = re.compile(r"\.{3,}|…{2,}")
# 标题尾部被 PDF 带出来的页码（「响应函 37」「资格审查资料 12」）。目录行与正文行都可能带，
# 是骨架比对失败的头号原因，统一在取标题时去掉。
_PAGE_TAIL = re.compile(r"[ \t　]+\d{1,3}[ \t　]*$")
# 一级小节的标题长度上限：**不能按「投标函」三个字去卡**。实测询比采购文件的第一项写成
# 「一、响应函（含响应函附录、报价明细表，格式见本页）」这类长标题，超过 40 字就被静默跳过，
# 于是**格式章第一节整项消失**（后面几项照旧出现，看起来像"第一项本来就不存在"）。
_SECTION_LINE = re.compile(r"^[ \t]*([一二三四五六七八九十]{1,3})[ \t]*[、.．][ \t]*([^\n]{1,60})$")
# **第一节**的兜底：格式章里的第一条「一、」不限标题长度（正文里那条常带一长串括号说明）。
# 只在「这一行是不是第一项」这一件事上用；其余小节仍走 `_SECTION_LINE`，免得把正文段落
# 当成小节标题。见 `_first_section_line`。
_FIRST_SECTION_LINE = re.compile(r"^[ \t]*([一二三四五六七八九十]{1,3})[ \t]*[、.．][ \t]*([^\n]{1,120})$")
# **编号位被 PDF 写成破折号**：「—、响应函」。U+2014 与「一」形近，字体替换/取字时会
# 把一个「一」抽成破折号。实测《新3.pdf》第六章第一项就是「—、响应函」——上面两条正则
# 都要求中文数字，于是**第一节整项消失**：`official_structure()` 第一项变成「二 授权委托书」，
# `format_document_section(…, "响应函")` 返回 None，整章退回模型自写（承诺条款全丢）。
# 只在「整章还没有任何『一、』」且这条破折号行排在所有带号小节之前时才还原成「一」：
# 正文里用「—、」写的项目符号不会被当成小节。
_DASH_SECTION_LINE = re.compile(r"^[ \t]*([—–－─―﹣])[ \t]*[、.．][ \t]*([^\n]{1,120})$")
# **二级**格式小节：「（一）基本情况表」「(二)近年财务状况表」。
# 第六章「八、资格审查资料」下面就是（一）~（八）这八个法定小节，而 `_SECTION_LINE`
# 只认「一、」形态，于是八个子节全被并进「八、」这一节的正文里，既没有独立粒度，
# 也没有任何地方能发现「（二）（五）（六）（八）根本没进成稿」。这条正则只用于
# **父节内部的子节切分**，不参与一级章节目录（那一层仍由 `_SECTION_LINE` 负责）。
_SUB_SECTION_LINE = re.compile(
    r"^[ \t]*[（(][ \t]*([一二三四五六七八九十]{1,3})[ \t]*[）)][ \t]*[、.．]?[ \t]*([^\n]{1,60})$")
# 「（一）小节标题 ...... 67」是目录行，不是小节本体；带点线/页码的一律不当标题。
_SUB_TITLE_TAIL = re.compile(r"(?:\.{2,}|…{2,})\s*\d{0,3}\s*$")
# 「附录／报价明细」类标题词：这些块在招标原文里**经常没有单独编号**，直接写在函件
# 正文后面（《新3.pdf》第 37 页就是「响应函附录」「报价明细」两行，下一行是表头）。
# `format_document_section(…, "响应函附录")` 要能只切出这一块，见 `_appendix_slice`。
_APPENDIX_WORDS = ("附录", "报价明细", "分项报价", "报价表", "开标一览")
# **编号单独占一行**：「（二）」下面一行才是标题。标题长到要换行、或编号在小节末页、
# 标题在下一页时，PDF 抽出来就是两行（实测《新3.pdf》第 41 页末尾是「（二）」，
# 第 42 页开头才是「信用中国失信被执行人、全国企业信用信息公示系统严重违法失信企业
# 查询记录」）。旧实现只认同行的「（二）标题」，于是这一节**根本不存在**：
# 资格审查资料只剩（一），`ensure_format_subsections` 看到 `len(subs) < 2` 直接返回，
# 两网查询记录整节（废标项）一个图框都没产出。
_SUB_MARKER_ONLY = re.compile(
    r"^[ \t]*[（(][ \t]*([一二三四五六七八九十]{1,3})[ \t]*[）)][ \t]*[、.．]?[ \t]*$")

# 「投标文件格式」章的标题特征。
# **不认采购方式用词**：询比采购/竞争性磋商把「投标文件」叫「响应文件」，把「投标函」叫
# 「响应函」，还有「报价文件格式」的写法。这里只按「有格式章」这个语义认，任何一种用词
# 都要认出来；最后一条「文件格式」是兜底（投标/响应/报价 都落在里面）。
_FORMAT_WORDS = ("投标文件格式", "响应文件格式", "投标文件的格式", "响应文件的格式",
                 "报价文件格式", "投标文件组成", "响应文件组成", "投标文件编制",
                 "响应文件编制", "投标文件内容", "响应文件内容", "格式及要求", "文件格式")
# 「供货/技术要求」章的标题特征
_TECH_CHAPTER_WORDS = ("供货要求", "技术要求", "技术规格", "技术规范", "技术标准",
                       "采购需求", "货物需求", "用户需求", "技术需求", "需求书")


def _cn_number(value: str) -> int:
    """把「一」「十二」「二十三」转成整数；认不出返回 0。"""
    text = str(value or "").strip()
    if not text:
        return 0
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text, 0)


# 把「第N章…」误判成章标题的典型形态：正文里的条款引用被 PDF 断成了单独一行，
# 例如投标函里的一句「第二章“投标人须知”第 1.4.3 项规定的任何一种情形。」
# 一旦当成章标题，第六章（投标文件格式）的正文就被切掉一大半，格式原文全部丢失。
_CLAUSE_REF_TITLE = re.compile(
    r"^(?:[“\"『《])|第[ \t]*\d+(?:\.\d+)*[ \t]*[条款项]|(?:\d+\.)+\d+|[，。；、]$"
)


def _is_chapter_heading(title: str) -> bool:
    """这行「第N章 xxx」是不是真章标题（而不是正文里的条款引用）。"""
    clean = str(title or "").strip(" 　\t")
    if not clean or len(clean) > 30:
        return False
    if _CLAUSE_REF_TITLE.search(clean):
        return False
    # 标题不会以句末标点收尾，也不会是一句完整的话
    if clean.endswith(("。", "！", "？", "：", ":", "；", ";")):
        return False
    if re.search(r"(?:规定|情形|条款|内容见|如下)[。；]", clean):
        return False
    return True


def chapter_spans(text: str) -> list[dict[str, Any]]:
    """按「第N章」切块：返回 [{label,no,title,body,line}]。

    目录行带点线/页码，不算章标题；正文里「第二章“投标人须知”第 1.4.3 项规定…」
    这类被 PDF 断行的条款引用也不算章标题（否则会把上一章正文拦腰截断）。
    同一章标题重复出现时保留全部，由调用方按「正文最长」挑。
    """
    lines = str(text or "").splitlines()
    marks: list[tuple[int, str, int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip(" 　\t")
        found = _CHAPTER_LINE.match(stripped)
        if not found or _DOT_LEADER.search(stripped):
            continue
        if not _is_chapter_heading(found.group(2)):
            continue
        marks.append((index, "第%s章" % found.group(1), _cn_number(found.group(1)),
                      found.group(2).strip()))
    spans: list[dict[str, Any]] = []
    for position, (start, label, number, title) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        spans.append({"label": label, "no": number, "title": title,
                      "body": "\n".join(lines[start + 1:end]), "line": start})
    return spans


def _pick_span(text: str, words: tuple[str, ...]) -> dict[str, Any] | None:
    """挑「标题命中 words 且正文最长」的那一章（目录行已排除）。"""
    hits = [span for span in chapter_spans(text)
            if span["title"] and any(word in span["title"] for word in words)]
    if not hits:
        return None
    return max(hits, key=lambda span: len(span["body"]))


def tech_chapter(text: str) -> dict[str, Any] | None:
    """供货/技术要求所在章（第五章、第七章都可能，没有就返回 None）。"""
    return _pick_span(text, _TECH_CHAPTER_WORDS)


def _title_format_span(text: str) -> dict[str, Any] | None:
    """按**标题词**认的格式章（`_FORMAT_WORDS` 里那串固定词）。"""
    return _pick_span(text, _FORMAT_WORDS)


def format_chapter(text: str) -> dict[str, Any] | None:
    """投标文件格式所在章（不假定章号）。

    两种认法，标题词优先：

    1. 标题里出现「投标文件格式 / 响应文件格式 / 文件格式」这类固定词（`_FORMAT_WORDS`）；
    2. **格式清单**：同一章里成串列出 `格式\\s*\\d+(-\\d+)?` 条目（`_catalog_span`）。

    第 2 条是实测事故逼出来的：公开招标《招标文件2.11开标(1).pdf》的格式章叫
    《第四章 投标文件有关格式》—— 标题里一个固定词都不含（「投标文件」与「格式」之间
    夹着「有关」），第 1 条判据整体落空，`format_chapter()` 返回 None，后面所有环节
    （`format_sections` / `official_structure` / `ensure_format_subsections`）全部空转，
    成稿里 13 个法定格式一个都没有。
    """
    span = _title_format_span(text)
    if span is not None:
        return span
    return _catalog_span(text)


def _title_key(title: str) -> str:
    """标题比对用的骨架：去括号说明（含单位负责人/适用情形）、去标点与空格。

    括号说明要**按嵌套反复剥、且不限 30 字**：询比采购文件的第一项常写成
    「响应函（含响应函附录、报价明细表、响应总报价（大写含税）、下浮率、承诺条款，
    格式详见本页）」这种长说明（还带内层括号）。旧的「只剥 30 字以内」会让正文这条的
    骨架变成「响应函下浮率承诺条款格式详见本页」，而目录那条是「响应函」——
    同一节因此被切成两条记录，`format_document_section()` 全等命中目录里那条**正文为空**
    的假小节，第一节于是「有标题、没正文」，整章退回模型自写。
    落单的左括号（PDF 断行造成的半括号）一律截断，与 `tender_outline._clean_title` 同口径。
    """
    text = str(title or "")
    for _ in range(3):          # 内层括号剥掉之后外层才配平，所以要反复剥
        text = re.sub(r"[（(][^（）()]{0,120}[）)]", "", text)
    text = re.split(r"[（(]", text)[0]
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
    return clean


def section_key(title: str) -> str:
    """标题比对骨架（去括号说明、去标点与空格）。对外暴露给格式小节完整性校验用。"""
    return _title_key(title)


def _catalog_related(key: str, catalog: set[str]) -> bool:
    """标题骨架与目录里某一条**相关**（全等或互相包含，短名如「响应函」也放行）。

    目录写「一、响应函及响应函附录」而正文写「一、响应函」是常态（PDF 排版与人工目录
    用语不同），全等比对会把这整节判成"文书内部条款"丢掉 —— 丢的正好是法定第一节。
    """
    if not key:
        return False
    if key in catalog:
        return True
    return any(_loose_key(key, other) for other in catalog)


def _catalog_keys(lines: list[str]) -> set[str]:
    """先认出「目录」里的那一串小节标题（一、二、…十三）。

    格式章里「一、」「二、」既用来分小节，也用来列**文书内部的条款**
    （承诺书里的一~六条、资格审查资料里的一~五项）。只按序号切会把父小节的正文
    切碎，承诺书的六条罚则全跑到外面去。所以先用目录（连续递增的那一串）定下
    哪些序号标题才是真小节，其余序号标题保留在父小节正文里。
    """
    keys: list[str] = []
    previous = 0
    for line in lines:
        found = _SECTION_LINE.match(line.strip(" 　\t"))
        if not found:
            continue
        number = _cn_number(found.group(1))
        title = found.group(2).strip(" 　\t。，,；;")
        # 目录行常带点线与页码（「投标函 ........ 49」）：页码不是标题的一部分，
        # 不去掉的话骨架永远对不上正文里那条同名标题，整章小节会被判成"内部条款"而全部丢失。
        if _DOT_LEADER.search(title):
            title = _DOT_LEADER.split(title)[0].strip()
        # 没有点线、只有页码的写法（「一、响应函 37」）同样会污染骨架：不去页码的话
        # 目录骨架是「响应函37」，正文标题骨架是「响应函」，两边永远对不上 ——
        # 于是**格式章所有正文小节都被判成"文书内部条款"而整批丢掉**，只剩目录里那条
        # 假小节。这是线上「第一节响应函整项消失」的确定性根因之一。
        title = _PAGE_TAIL.sub("", title).strip()
        key = _title_key(title)
        if not key or not number:
            continue
        # 「递增」而不是「必须 +1」：真实格式章经常缺号（一、二、三、四、八、九 …），
        # 按 +1 卡会把缺号之后的小节全部踢出目录骨架，父节本身也可能被判成"内部条款"，
        # 于是「八、资格审查资料」整节消失 —— 它下面挂的正是废标级的八项法定材料。
        # 同号重复仍按标题区分（「二 法定代表人身份证明」与「二 授权委托书」是两件文书）。
        if number > previous or (number == previous and key not in keys):
            keys.append(key)
            previous = number
        elif number == 1 and len(keys) >= 3:
            break          # 已经过了目录（又回到「一、」），后面的是正文内部条款
        if len(keys) >= 16:
            break
    return set(keys) if len(keys) >= 3 else set()


def _restore_dashed_ordinal(lines: list[str]) -> list[str]:
    """把编号位写成破折号的**第一项**还原成「一、」；其余行一个字不动。

    为什么要有这一步（实测，不是推测）：《新3.pdf》（山西新元煤炭 2026 年度中小修理
    四标段，48 页）第 36 页的格式章第一项抽出来是「—、响应函」（U+2014 + 顿号）。
    `_SECTION_LINE` / `_FIRST_SECTION_LINE` 只认中文数字，这一行整行匹配不上，于是：

    * `official_structure()` 的第一项是「二 授权委托书」——评审先看的第一页没有；
    * `format_sections()` 里没有「响应函」，`format_document_section(…, "响应函")` 返回 None，
      整章退回模型自写，七条承诺与报价变量一条都不在；
    * 紧跟着的「响应函附录／报价明细」也就无处可挂。

    还原条件（三条同时满足，避免把正文里的破折号项目符号当小节）：

    1. 整章**没有任何**「一、」形态的小节（有真的就不用猜）；
    2. 这条破折号行是它**第一次**出现，且排在所有带号小节（「二、」「三、」…）之前；
    3. 破折号只有一个字符（「——、」是排版分隔线，不是编号位）。

    只改编号位那一个字符，标题与正文逐字保留。
    """
    numbered: list[int] = []
    for index, line in enumerate(lines):
        found = _SECTION_LINE.match(str(line or "").strip(" 　\t"))
        if not found:
            continue
        if _cn_number(found.group(1)) == 1:
            return lines           # 已经有真的「一、」：破折号只能是项目符号
        numbered.append(index)
    if not numbered:
        return lines               # 一条带号小节都没有：无从判断顺序
    for index in range(numbered[0]):
        stripped = str(lines[index] or "").strip(" 　\t")
        found = _DASH_SECTION_LINE.match(stripped)
        if not found:
            continue
        return (list(lines[:index]) + ["一" + stripped[len(found.group(1)):]]
                + list(lines[index + 1:]))
    return lines


def _format_lines(text: str) -> tuple[list[str], set[str]]:
    """格式章正文行 + 目录里那串真小节标题的骨架（找不到格式章时是 ([], set())）。"""
    span = format_chapter(text)
    if span is None:
        return [], set()
    lines = _restore_dashed_ordinal(span["body"].splitlines())
    return lines, _catalog_keys(lines)


def _mark_title(title: str) -> str:
    """一条「一、xxx」标题行里的标题本体：去目录点线、去被带出来的页码、去尾巴标点。

    目录行与正文行都会被这条正则命中，目录行常写成「一、响应函…………37」或
    「一、响应函 37」。不洗掉的话同一节会出现两条**键不同**的记录（响应函 / 响应函37），
    既重复又把真正文挡在后面。
    """
    clean = str(title or "").strip(" 　\t。，,；;")
    if _DOT_LEADER.search(clean):
        clean = _DOT_LEADER.split(clean)[0]
    clean = _PAGE_TAIL.sub("", clean)
    return clean.strip(" 　\t。，,；;")


# ══════════════════════════════════════════════════════════════════════════
# 「格式1 投标函」清单：章节里逐条列出的 格式1~格式12
# ══════════════════════════════════════════════════════════════════════════
# 实测事故（公开招标《招标文件2.11开标(1).pdf》，山西兰花唐安煤矿端头及超前支护技术
# 研究与应用，LHZCDL-2601002）：第四章《投标文件有关格式》（第 36~46 页）用
# 「格式1 投标函」「格式2-1 法定代表人身份证明书」「格式2-2 法定代表人授权书」
# …「格式12 投标保证金凭证」逐条列出**13 个法定格式**（12 个编号，其中 2 编出 2-1/2-2
# 两条）。现有两条判据同时落空：
#
# 1. `_FORMAT_WORDS` 只认标题里出现「投标文件格式 / 响应文件格式 / 报价文件格式 /
#    文件格式」这类固定词。「投标文件有关格式」中间夹着「有关」，一个都不含
#    → `format_chapter()` 返回 None，整章被当成"这份文件没有格式章"；
# 2. `_SECTION_LINE`（只认「一、」）、`_catalog_keys`（同样只喂 `_SECTION_LINE`）、
#    `_section_marks`（只认「一、」/「（一）」）都不认「格式1 投标函」这种
#    「编号是阿拉伯数字 + 短横线」的条目 → 即使章找到了，13 条里一条也进不了
#    `format_sections()`；实测把章标题改成「第四章 投标文件格式」后，`format_sections()`
#    只返回「资格文件 / 商务文件 / 技术部分」三个段标题，13 个格式仍然全部丢失。
#
# 所以这里按**形状**认清单：同一章里成串出现 ≥3 条以 `格式*N[(-N)]` 开头的条目，
# 即认定该章是"投标文件格式"章；条目名取「格式N」之后的名称（`格式2-1` 连短横线一起认）。
_CATALOG_ITEM = re.compile(
    r"^[ \t　]*格式[ \t　]*(\d{1,2}(?:[ \t　]*[-－—–][ \t　]*\d{1,3})?)"
    r"[ \t　]*[、.．:：\-－—–]*[ \t　]*([^\n]{1,80})$")
# 条目名不可能是这些开头：「格式10 规定…」「格式3 的要求」「格式12 详见…」「格式10 第一章」
# 都是交叉引用，不是清单条目。`格式条款`「形式审查」连 `格式N` 都不匹配，天然出局。
# 「第」只在后面跟「章/节/条/项/款/部分」时才算引用（「格式10 第一章…」）；
# 「格式11 第三方检测报告」「格式10 见证取样记录」都是真格式名，不能因为开头是
# 「第 / 见」就整条丢掉。
_CATALOG_TITLE_BAD = re.compile(
    r"^(?:的|中|所|内|规定|要求|说明|填写|按|如|及|与|和|或"
    r"|第[一二三四五六七八九十\d]{1,3}(?:[章节条项款]|部分))")
# 标题不会以句读收尾（「格式3 投标总报价表」可以，「格式3 详见第二章，」不行）。
# 冒号不算：真实排版的条目名常写成「格式3 投标总报价表：」，卡掉等于整条丢失。
_CATALOG_TITLE_TAIL = re.compile(r"[。；;，,]\s*$")
_CATALOG_MIN_ITEMS = 3


def _catalog_heading(line: str) -> dict[str, str] | None:
    """一行是不是「格式N 名称」清单条目；是则返回 {no,title,label}。"""
    raw = str(line or "").strip(" 　\t")
    if not raw or len(raw) > 90:
        return None
    found = _CATALOG_ITEM.match(raw)
    if not found:
        return None
    no = re.sub(r"[ \t　]", "", found.group(1))
    no = re.sub(r"[-－—–]", "-", no)
    title = _mark_title(found.group(2))
    if not title or len(title) > 60:
        return None
    if _CATALOG_TITLE_BAD.match(title) or _CATALOG_TITLE_TAIL.search(title):
        return None
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", title):
        return None
    return {"no": no, "title": title, "label": "格式%s %s" % (no, title)}


def _catalog_entries(lines: list[str]) -> list[dict[str, Any]]:
    """章正文里全部「格式N 名称」条目（含目录里重复出现的那一份），每条带原文正文。"""
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        found = _catalog_heading(line)
        if not found:
            continue
        entries.append({**found, "line": index})
    for position, entry in enumerate(entries):
        end = entries[position + 1]["line"] if position + 1 < len(entries) else len(lines)
        entry["end"] = end
        entry["body"] = "\n".join(lines[entry["line"] + 1:end]).strip()
    return entries


def _catalog_score(run: list[dict[str, Any]]) -> tuple[int, int]:
    return (len(run), sum(len(str(entry.get("body") or "")) for entry in run))


def _catalog_list(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """成串的清单条目：同一「格式N」只留正文最长的那条；不足 3 条不算清单。

    **不按"编号递增"切段**（旧 `_catalog_keys` 就是那样卡「一、」目录的）：实测事故文件
    按三段归位 —— 资格文件先出「格式2-1 / 格式2-2 / 格式12」，商务文件才出「格式1 /
    格式3 … 格式9」，全局编号在回退。按递增切会把资格文件那三条整段切掉，13 个格式只剩
    10 个。判据因此只用"同一章里成串出现 ≥3 条 `格式N 名称`"；目录里重复出现的那一份按
    「格式N」去重，取正文更厚的一处（目录那条正文为空）。
    """
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in entries:
        no = str(entry.get("no") or "")
        if no not in best:
            order.append(no)
            best[no] = entry
            continue
        if len(str(entry.get("body") or "")) > len(str(best[no].get("body") or "")):
            best[no] = entry
    run = [best[no] for no in order]
    return run if len(run) >= _CATALOG_MIN_ITEMS else []


def _catalog_span(text: str) -> dict[str, Any] | None:
    """按「格式清单」认章（标题里没有「投标文件格式」这类固定词时的兜底）。"""
    spans = chapter_spans(text)
    best: tuple[tuple[int, int], dict[str, Any]] | None = None
    for span in spans:
        run = _catalog_list(_catalog_entries(span["body"].splitlines()))
        if not run:
            continue
        score = _catalog_score(run)
        if best is None or score > best[0]:
            best = (score, span)
    if best is not None:
        return best[1]
    if spans:
        return None          # 有章结构就只在章内认，别把不同章的条目拼成一份假清单
    # 原文完全没有「第N章」（纯文本粘贴 / 解析丢了章标题）时才退回全文认一次
    run = _catalog_list(_catalog_entries(str(text or "").splitlines()))
    if not run:
        return None
    return {"label": "", "no": 0, "title": "", "body": str(text or ""), "line": 0}


# ── 三段结构：投标文件由商务文件、资格文件和技术文件三部分组成 ────────────────────
# 事故原文（第二章第 8 条 / 第 12.5 条）：「投标文件依序分别由商务文件、资格文件和技术
# 文件三部分组成，三部分装订成一册」。成稿封面只写「投标文件（技术部分）」—— 商务与
# 资格两段整体消失。段名与顺序一律从这句话取，取不到就什么都不加（绝不自己编三段）。
_PART_TOKEN = r"[\u4e00-\u9fff]{2,8}(?:文件|部分)"
_COMPOSE_PARTS = re.compile(
    r"(%s(?:[ \t　]*[、，,和及与][ \t　]*%s)+)[ \t　]*(?:[三四两二五六]部分|组成)"
    % (_PART_TOKEN, _PART_TOKEN))
_PART_SPLIT = re.compile(r"[、，,和及与]")
_PART_LEAD = re.compile(r"^.*(?:由|分别|依序|依次|组成|构成|包括|分为|是)")
_PART_SENTENCE = re.compile(r"(?:投标|响应|报价)文件[^。\n]{0,120}?[三四两二五六]部分")


def _part_names(phrase: str) -> list[str]:
    names: list[str] = []
    for raw in _PART_SPLIT.split(str(phrase or "")):
        token = _PART_LEAD.sub("", str(raw or "").strip(" 　\t"))
        token = token.strip(" 　\t。，,；;：:")
        if not token or len(token) > 10 or not re.search(r"(?:文件|部分)$", token):
            continue
        if token not in names:
            names.append(token)
    return names


def tender_parts(text: str) -> list[str]:
    """原文写明的投标文件组成段（「商务文件、资格文件和技术文件」），按原文顺序。

    只认原文那句话，解析不到返回 []。调用方据此把格式按段归位；没有原文依据时
    **不猜**（宁可不出三段标题，也不替招标人编一个投标文件结构）。
    """
    source = str(text or "")
    if not source.strip():
        return []
    for found in _COMPOSE_PARTS.finditer(source):
        names = _part_names(found.group(1))
        if len(names) >= 2:
            return names
    # 换一种写法（段名在前 / 中间夹了别的词）：按句子再找一次
    for sentence in re.split(r"[。；;\n]", source):
        if not _PART_SENTENCE.search(sentence) or "文件" not in sentence:
            continue
        names = [name for name in _part_names(_PART_SPLIT.sub("、", sentence))
                 if name not in ("投标文件", "响应文件", "报价文件")]
        if len(names) >= 2:
            return names
    return []


def _part_stem(name: str) -> str:
    """段名的词干：「技术文件」「技术部分」都归到「技术」，两种写法才能对上。"""
    return re.sub(r"(?:文件|部分|标书|册)$", "", re.sub(r"[\s　]", "", str(name or "")))


def _part_in_line(line: str, parts: list[str]) -> str:
    """一行是不是某一段的标题行（「一、资格文件」「（二）商务文件：」），是则返回段名。"""
    text = str(line or "").strip(" 　\t")
    if not text or len(text) > 30:
        return ""
    text = re.sub(r"^第[一二三四五六七八九十\d]{1,2}部分[ \t　、.．:：]*", "", text)
    text = re.sub(r"^[（(][一二三四五六七八九十\d]{1,3}[）)][ \t　]*[、.．:：]?[ \t　]*", "", text)
    text = re.sub(r"^[一二三四五六七八九十]{1,3}[ \t　]*[、.．][ \t　]*", "", text)
    text = re.sub(r"^\d{1,2}[ \t　]*[、.．][ \t　]*", "", text)
    text = re.split(r"[（(]", text)[0]
    text = text.strip(" 　\t：:、.．。，,")
    if not text or len(text) > 12:
        return ""
    stem = _part_stem(text)
    if len(stem) < 2:
        return ""
    for part in parts:
        if stem == _part_stem(part):
            return part
    return ""


def _assign_parts(entries: list[dict[str, Any]], lines: list[str],
                  parts: list[str]) -> None:
    """给每个格式条目标出它属于哪一段（资格/商务/技术），标不出就留空。"""
    for entry in entries:
        entry["part"] = ""
    if not parts or not entries:
        return
    marks = [(index, name) for index, line in enumerate(lines)
             if (name := _part_in_line(line, parts))]
    for entry in entries:
        part = ""
        for index, name in marks:
            if index >= entry["line"]:
                break
            part = name
        if not part:
            # 段标题也可能写在条目正文开头（「格式2-1 …（资格文件）」这类）
            for raw in str(entry.get("body") or "").splitlines()[:20]:
                part = _part_in_line(raw, parts)
                if part:
                    break
        entry["part"] = part


# ── 原文要点：比例/金额/数量这类硬约束，逐句照抄 ──────────────────────────────
# 「研发费不低于项目总投资15%」「随机备件不低于设备总价3%」这种约束是废标/丢分线，
# 补骨架时必须写出来；**只从原文抽**，抽不到就不写（绝不拿一个通用百分比顶上）。
_POINT_HINT = re.compile(r"不低于|不得低于|不少于|不得少于|不小于|不高于|不得超过"
                         r"|不超过|至少|最低|最多|≥|≤")
_POINT_LEAD = re.compile(r"^[（(]?\d{1,2}[）).．、][ \t　]*"
                         r"|^[一二三四五六七八九十]{1,2}[、.．][ \t　]*")


def _catalog_points(body: str) -> list[str]:
    """条目原文里的硬性约束（带数字的比例/金额/数量），按原文句子返回。"""
    points: list[str] = []
    seen: set[str] = set()
    for raw in str(body or "").splitlines():
        for sentence in re.split(r"[。；;]", raw):
            text = re.sub(r"^[ \t　|｜]+|[ \t　|｜]+$", "", sentence)
            text = _POINT_LEAD.sub("", text).strip()
            text = re.sub(r"[ \t　]{2,}", " ", text)
            if len(text) < 5 or len(text) > 160:
                continue
            ratio = bool(re.search(r"\d", text) and re.search(r"[%％]", text))
            if not (_POINT_HINT.search(text) or ratio):
                continue
            # 约束句即使没给数字（「不低于规定比例」）也照抄：它是原文的硬约束，只是
            # 解析不到数值 —— 此时**不补数**，绝不会凭空写一个百分比上去。
            key = re.sub(r"[\s　]", "", text)
            if key in seen:
                continue
            seen.add(key)
            points.append(text)
            if len(points) >= 8:
                return points
    return points


def _catalog_data(text: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """格式清单章 + 带「所属段 / 原文要点」的条目；不是清单章时条目为 []。"""
    span = format_chapter(text)
    if span is None:
        return None, []
    lines = _restore_dashed_ordinal(span["body"].splitlines())
    entries = _catalog_list(_catalog_entries(lines))
    if len(entries) < _CATALOG_MIN_ITEMS:
        return span, []
    parts = tender_parts(text)
    _assign_parts(entries, lines, parts)
    for entry in entries:
        entry["points"] = _catalog_points(entry["body"])
    return span, entries


def format_catalog(text: str) -> list[dict[str, Any]]:
    """招标文件「格式清单」章的条目（格式1~格式12）；不是清单章返回 []。

    每条：{no, title, label("格式N 名称"), body(该条目原文), points(原文硬约束), part(所属段)}。
    供 `format_subsections.ensure_format_subsections` 补骨架用 —— 条目一个都不能少。
    """
    _span, entries = _catalog_data(text)
    return entries


def _catalog_sections(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清单条目 → 与 `format_sections()` 同形状的小节记录（label 带「格式N」）。"""
    return [{"label": entry["label"], "ordinal": entry["no"],
             "title": entry["title"], "body": entry["body"]} for entry in entries]


_CN_ORDINAL_TEXT = "一二三四五六七八九十"


def _cn_text(number: int) -> str:
    if 1 <= number <= 10:
        return _CN_ORDINAL_TEXT[number - 1]
    if 10 < number < 20:
        return "十" + _CN_ORDINAL_TEXT[number - 11]
    return str(number)


def _catalog_structure(entries: list[dict[str, Any]], parts: list[str]) -> list[str]:
    """格式清单 → 目录骨架：先按段（商务/资格/技术）分段标题，再逐条列格式。"""
    items: list[str] = []
    ordered = [part for part in parts if any(e.get("part") == part for e in entries)]
    if not ordered:
        labels = [str(entry.get("label") or "") for entry in entries]
        if not parts:
            return labels
        # 原文写了三段、清单里却没有段标题可归位：三段标题照样出（目录里能看见
        # 「商务文件 / 资格文件 / 技术文件」），条目按原文顺序列在后面 ——
        # **不猜归属**（把资格材料放进技术部分比不分段更糟），但也绝不只剩「技术部分」。
        return (["%s %s" % (_cn_text(index), part)
                 for index, part in enumerate(parts, start=1)] + labels)
    for ordinal, part in enumerate(ordered, start=1):
        items.append("%s %s" % (_cn_text(ordinal), part))
        items.extend(str(entry.get("label") or "") for entry in entries
                     if entry.get("part") == part)
    # 归不进任何一段的条目照原文顺序列在最后：宁可不分段，也不丢条目
    items.extend(str(entry.get("label") or "") for entry in entries if not entry.get("part"))
    return items


def _first_section_line(lines: list[str]) -> int:
    """格式章正文里**第一条**「一、」的行号（不限标题长度）；没有则 -1。

    `_SECTION_LINE` 给标题留了长度上限（防正文段落误命中），但格式章的第一项常写成
    「一、响应函（含响应函附录、报价明细表、响应总报价（大写含税）与下浮率、承诺条款，
    格式详见本页…）」这种一长串括号说明 —— 上限恰好把这一整行否掉，于是**第一项消失、
    后面几项照旧出现**（看起来像"原文本来就没有第一项"）。所以第一条单独放行：
    它是不是条款与长度无关，它在最前面、它就是这个章的第一项。
    """
    for index, line in enumerate(lines):
        if _FIRST_SECTION_LINE.match(line.strip(" 　\t")):
            return index
    return -1


def _section_marks(lines: list[str], catalog: set[str]) -> list[tuple[int, str, str]]:
    """一级小节（「一、」形态）的标题行：[(行号, 中文序号, 标题)]。

    命中目录骨架才算小节：承诺书内部的「一、~六、」是条款，不是小节。

    两条硬保护（线上「格式章第一节整项消失」的回归口子）：

    * **第一节永远是小节**：格式章的第一条「一、」不可能是文书内部条款 —— 条款只会出现在
      某个小节正文里、位于该小节标题之后。所以即使目录骨架没对上（目录写成「一、响应函及
      附录」而正文写成「一、响应函」、或目录被 PDF 拆行、或标题长到超过上限），第一节也必须留下。
    * **骨架按"相关"比对**：目录与正文对同一节的措辞常有一两个字的差别（响应函 / 响应函及
      响应函附录），全等比对会把正文那一节整条判成"内部条款"丢掉。改成骨架互相包含即算命中，
      短名（响应函只有 3 个字）也放行 —— 全等优先的语义不变，只是不再要求逐字相同。
    """
    marks: list[tuple[int, str, str]] = []
    # 格式章正文里的**第一条**「一、」：法定格式的第一项，无论叫什么名字、多少字都必须留下。
    first_line = _first_section_line(lines)
    for index, line in enumerate(lines):
        found = _SECTION_LINE.match(line.strip(" 　\t"))
        if not found and index == first_line:
            found = _FIRST_SECTION_LINE.match(line.strip(" 　\t"))
        if not found:
            continue
        title = _mark_title(found.group(2))
        key = _title_key(title)
        if not key:
            continue
        if catalog and key not in catalog and not _catalog_related(key, catalog) \
                and index != first_line:
            continue       # 文书内部的条款（一、二、…）不算小节
        marks.append((index, found.group(1), title))
    if marks:
        return marks
    # 有些招标文件的第六章直接用「（一）（二）…」当**一级**小节（整章没有「一、」）。
    # 这时不能整章判成"没有格式"：那等于每一个格式小节都从大纲里消失。
    # 只在一条「一、」都找不到时才启用，正常情况下不影响任何既有行为。
    candidates = _subsection_marks(lines)
    return [(item["line"], item["ordinal_text"], item["title"])
            for item in _monotonic_run(candidates)]


def format_sections(text: str) -> list[dict[str, Any]]:
    """投标文件格式章里的每个「一、二、…」小节及其正文（含小节内部的条款原文）。

    同名小节在目录与正文各出现一次（目录那次正文为空），按标题骨架去重后取正文最长的那处；
    「二、法定代表人身份证明」和「二、授权委托书」编号相同但是两件不同的文书，靠标题区分。

    **格式清单优先**：章里成串列出 ≥3 条「格式N 名称」时，条目就是一级格式，直接按清单
    返回（`label` 带「格式N」）。实测事故文件把段标题写成「一、资格文件 / 二、商务文件 /
    三、技术部分」、格式条目写成「格式1 投标函」：只按「一、」切分的话，返回的是三个段
    标题，13 个法定格式一条都不剩 —— 粒度错了，下游也就无从补起。
    """
    lines, catalog = _format_lines(text)
    if not lines:
        return []
    entries = _catalog_list(_catalog_entries(lines))
    if entries:
        return _catalog_sections(entries)
    marks = _section_marks(lines, catalog)
    best: dict[str, dict[str, Any]] = {}
    for position, (start, ordinal, title) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        key = _title_key(title)
        if not key:
            continue
        # **空正文的小节也要留下**：招标文件只给了标题、正文在下一页/在另一处时，旧写法
        # `len(body) > 0` 直接不收，等于这一项从目录里静默消失（第一节最容易中招）。
        # 正文更长的优先；长度相同取靠后的那处（目录在前、正文在后，与 `_monotonic_run` 同口径）。
        if key not in best or len(body) >= len(str(best[key].get("body") or "")):
            best[key] = {"label": "%s、%s" % (ordinal, title), "ordinal": ordinal,
                         "title": title, "body": body}
    return list(best.values())


def _loose_key(left: str, right: str) -> bool:
    """两个标题骨架是不是同一节：全等，或一方包含另一方且都不短于 3 字。

    门槛从 4 字降到 3 字：「响应函」「报价函」「投标函」都只有 3 个字，旧门槛让它们
    永远配不上原文里「响应函（附录：报价明细）」这类带说明的标题 —— 第一章取不到格式原文，
    只能退回占位或让模型自己编，正是「整个响应函没了」的直接原因。
    """
    a, b = str(left or ""), str(right or "")
    if not a or not b:
        return False
    if a == b:
        return True
    return (a in b or b in a) and min(len(a), len(b)) >= 3


def _sub_heading(ordinal_text: str, title: str, heading: str) -> dict[str, Any] | None:
    """二级小节标题的校验与构造（单行、拆成两行两种来源共用同一套门槛）。"""
    clean = str(title or "").strip(" 　\t")
    ordinal = _cn_number(str(ordinal_text or ""))
    if not ordinal or not clean or len(clean) > 40:
        return None
    # 「（一）不与其他投标人串通投标，…；」是小节内部的条款，不是小节标题
    if clean.endswith(("。", "；", ";", "，", ",", "：", ":")):
        return None
    return {"ordinal": ordinal, "ordinal_text": str(ordinal_text),
            "title": clean, "heading": heading}


def subsection_heading(line: str) -> dict[str, Any] | None:
    """一行是不是「（一）基本情况表」这种二级小节标题；是则返回 {ordinal,title,heading}。

    目录行（带点线/页码）与承诺书内部条款（以句读收尾）都不算标题：
    前者不是正文，后者是某个小节内部的条文。

    只看**这一行**。编号与标题被 PDF 拆成两行时用 `subsection_heading_at()`。
    """
    text = str(line or "").strip(" 　\t")
    if not text or _DOT_LEADER.search(text) or _SUB_TITLE_TAIL.search(text):
        return None
    found = _SUB_SECTION_LINE.match(text)
    if not found:
        return None
    return _sub_heading(found.group(1), found.group(2), text)


def _looks_like_heading_title(title: str) -> bool:
    """拆行标题的**第二行**是不是标题本体（不是表格行、不是数据行、不是正文段落）。

    判据保守：没有竖线、没有多列空格、至少两个汉字、不以数字/句读开头收尾。
    为的是「（二）」下一行正好是表头/数据行时，不把小节标题读成「供应商名称 注册地址」。
    """
    clean = str(title or "").strip(" 　\t")
    if not clean or "|" in clean or re.search(r"[ \t]{2,}", clean):
        return False
    if re.match(r"^[\d\s.．、,，%¥￥]+$", clean):
        return False
    if len(re.findall(r"[\u4e00-\u9fff]", clean)) < 2:
        return False
    return True


def subsection_heading_at(lines: list[str], index: int,
                          stop: int | None = None) -> tuple[dict[str, Any] | None, int]:
    """第 `index` 行是不是二级小节标题；返回 (标题|None, 正文起始行号)。

    比 `subsection_heading()` 多认一种真实排版：**编号单独一行、标题在下一行**
    （《新3.pdf》第 41 页末尾「（二）」、第 42 页开头才是那串渠道名）。这种拆行不是
    特例 —— 标题一长、或小节标题跨页时，抽出来就是两行；只认同行会让整节消失。
    合并后的 `heading` 是**一行**「（二）标题」，再解析时走单行分支，天然幂等。
    """
    total = len(lines) if stop is None else min(int(stop), len(lines))
    if index < 0 or index >= total:
        return None, index + 1
    stripped = str(lines[index] or "").strip(" 　\t")
    found = _SUB_SECTION_LINE.match(stripped)
    if found:
        return _sub_heading(found.group(1), found.group(2), stripped), index + 1
    marker = _SUB_MARKER_ONLY.match(stripped)
    if not marker:
        return None, index + 1
    for follow in range(index + 1, total):
        title = str(lines[follow] or "").strip(" 　\t")
        if not title:
            continue                     # 编号与标题之间可能隔着空行
        if _SUB_SECTION_LINE.match(title) or _SUB_MARKER_ONLY.match(title):
            return None, index + 1       # 下一行又是编号：这是没有标题的空小节
        if not _looks_like_heading_title(title) or _DOT_LEADER.search(title):
            return None, index + 1
        merged = stripped + title
        return _sub_heading(marker.group(1), title, merged), follow + 1
    return None, index + 1


def _subsection_marks(lines: list[str], start: int = 0,
                      end: int | None = None) -> list[dict[str, Any]]:
    """区间内全部二级小节标题：[{ordinal,title,heading,line,body_start}]。"""
    total = len(lines) if end is None else min(int(end), len(lines))
    marks: list[dict[str, Any]] = []
    index = max(int(start), 0)
    while index < total:
        found, body_start = subsection_heading_at(lines, index, total)
        if found:
            marks.append({**found, "line": index, "body_start": body_start})
            index = max(body_start, index + 1)
        else:
            index += 1
    return marks


def _monotonic_run(marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """取**编号严格递增**的那一段（就是真小节清单）。

    为什么不能全收：承诺书里常见「（一）不围标…（二）不串标…」，它们和
    「（一）基本情况表」长得一模一样，但编号是从 1 重新开始的。真正的同级小节
    编号只会递增（（一）→（二）→…），所以按"递增"切段，取最长的一段；
    同样长时取靠后的那段 —— 目录在前、正文在后（与 `locate_section_pages` 同口径）。
    """
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous = 0
    for mark in marks:
        if mark["ordinal"] > previous:
            current.append(mark)
        else:
            if current:
                runs.append(current)
            current = [mark]
        previous = mark["ordinal"]
    if current:
        runs.append(current)
    if not runs:
        return []
    return max(runs, key=lambda run: (len(run), run[0]["line"]))


def format_sub_sections(text: str, parent_title: str = "") -> list[dict[str, Any]]:
    """格式章某一节下面的**二级小节**（「（一）…」形态）及其正文，按原文顺序返回。

    为什么单列这一层：第六章「八、资格审查资料」下面有（一）~（八）八个法定小节，
    `format_sections()` 只认「一、」形态，于是八个子节被并进父节正文 —— 粒度错了，
    下游（结构化直出只保留检出表格的那几页）就会整段丢掉没有表格的小节，
    而**没有任何地方能发现丢了哪几个**。有了这一层，缺哪个、补哪个都是确定的。

    `parent_title` 给定时只返回该一级小节下的子节（标题骨架比对，容忍带不带编号）；
    找不到格式章 / 该节没有子节时返回空列表。
    """
    lines, catalog = _format_lines(text)
    if not lines:
        return []
    marks = _section_marks(lines, catalog)
    if not marks:
        return []
    wanted = _title_key(parent_title) if str(parent_title or "").strip() else ""
    sections: list[dict[str, Any]] = []
    for position, (start, ordinal, title) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        if wanted and not _loose_key(_title_key(title), wanted):
            continue
        candidates = _subsection_marks(lines, start + 1, end)
        run = _monotonic_run(candidates)
        for offset, item in enumerate(run):
            stop = run[offset + 1]["line"] if offset + 1 < len(run) else end
            # 正文从标题之后开始：拆行标题多占一行（「（二）」+标题），不能把标题当正文。
            item["body"] = "\n".join(lines[item["body_start"]:stop]).strip()
            item["label"] = "%s、%s" % (ordinal, title)
            item["parent"] = title
            sections.append(item)
    return sections


def format_document_section(text: str, title: str) -> dict[str, Any] | None:
    """按章节标题找招标文件里对应的那份文书（格式原文）。

    章节名是从格式章标题清洗出来的（去掉了括号说明），这里用同样的骨架比对。
    找不到返回 None —— 说明招标文件只列了名称、没给格式原文。

    全等命中也要看正文：格式章里同一节常出现两次（目录行 + 正文行），目录那条
    **正文为空**。旧写法一命中就返回，于是第一次的「响应函」取到的是一行目录，
    渲染出空串 → 这一章没有格式原文（第一节"有标题、没正文"）。全等那条正文为空/
    极短时继续在相关的候选里取正文最长的一条：真正的格式原文永远比目录行有料。
    """
    key = _title_key(title)
    if not key:
        return None
    sections = format_sections(text)
    best = None
    for section in sections:
        if _title_key(section["title"]) != key:
            continue
        if best is None or len(str(section.get("body") or "")) > len(str(best.get("body") or "")):
            best = section
    if best is not None and len(re.sub(r"\s", "", str(best.get("body") or ""))) >= 20:
        return best
    related = [section for section in sections if _loose_key(key, _title_key(section["title"]))]
    if related:
        # 骨架相关的可能不止一条（目录里写「响应函及响应函附录」、正文里写「响应函」）：
        # 取正文最长的那条 —— 那才是真正的格式原文，目录行那条只是几个标题串。
        section = max(related, key=lambda item: len(str(item.get("body") or "")))
        return _appendix_slice(section, key) or section
    return best


def _appendix_slice(section: dict[str, Any], key: str) -> dict[str, Any] | None:
    """附录/报价明细**没有单独编号**时，从函件正文里切出那一块。

    实测：《新3.pdf》第 37 页的「响应函附录／报价明细」直接写在「—、响应函」这一节里
    （原文没给它编号），所以 `format_sections()` 里没有它这一条。问「响应函附录」的格式
    原文时，骨架相关的只有「响应函」这一节 —— 直接整段返回，等于把整封函（七条承诺 +
    落款）当成附录，附录表反而混在里面。

    这里按正文里的那一行标题（含「附录／报价明细／分项报价／报价表／开标一览」且不是
    句子）切片：切出来的正文以附录标题开头、到本节末尾为止。找不到这样一行就返回 None，
    由调用方退回整节（不硬造）。
    """
    if not any(word in key for word in _APPENDIX_WORDS):
        return None
    lines = str(section.get("body") or "").splitlines()
    for index, line in enumerate(lines):
        clean = str(line or "").strip(" 　\t")
        if not clean or len(clean) > 40 or re.search(r"[。；;]", clean):
            continue
        if not any(word in clean for word in _APPENDIX_WORDS):
            continue
        tail = "\n".join(lines[index:]).strip()
        if len(re.sub(r"\s", "", tail)) < 10:
            continue       # 只有一行标题、后面什么都没有：那是空引用，不切
        return {**section, "body": tail}
    return None


def section_headings(text: str, chapter_no: int = 0) -> list[str]:
    """抽出某一章的「一、二、三」小节标题（不带编号）。

    `chapter_no` 为 0 时自动找供货/技术要求章；找不到返回空。
    """
    span = None
    if chapter_no:
        span = next((s for s in chapter_spans(text) if s["no"] == chapter_no), None)
    span = span or tech_chapter(text)
    if span is None:
        return []
    found: list[str] = []
    for line in span["body"].splitlines():
        match = _SECTION_LINE.match(line.strip(" 　\t"))
        if not match:
            continue
        title = match.group(2).strip(" 　\t：:")
        if title and title not in found:
            found.append(title)
    return found


def official_structure(text: str) -> list[str]:
    """抽出「投标文件格式」章要求的章节目录（一、二、…）。

    这一节是**招标人规定的投标文件骨架**：大纲要和它对得上，否则组装时对不上号。
    章号不写死（第六/第七章都可能）；找不到这一章就返回空，由体检如实报告。
    只认「真正的章标题行」——目录里的同名条目带 `..... 49` 点线，不是正文。
    编号一旦回退（十三之后又出现「一」，是附件格式重新编号）就停止。

    **格式清单优先**：章里成串列出 ≥3 条「格式N 名称」时，目录就是这些条目，`label` 写成
    「格式N 名称」。原文写明「投标文件由商务文件、资格文件和技术文件三部分组成」时，
    先出三个段标题（一 资格文件 / 二 商务文件 / 三 技术文件 …），条目按段归位 ——
    成稿目录因此是**三段结构 + 13 个格式**，而不是只剩一个「技术部分」。
    """
    span = format_chapter(text)
    if span is None:
        return []
    # 编号位被写成破折号的第一项（「—、响应函」）先还原成「一、」，否则整项从大纲里消失。
    lines = _restore_dashed_ordinal(span["body"].splitlines())
    entries = _catalog_list(_catalog_entries(lines))
    if entries:
        parts = tender_parts(text)
        _assign_parts(entries, lines, parts)
        return _catalog_structure(entries, parts)
    items: list[str] = []
    last_ordinal = 0
    # 第一条「一、」单独放行：格式章第一项常带一长串括号说明，超过 `_SECTION_LINE` 的
    # 长度上限就整行匹配不上、整项从大纲里消失（后面的项照旧出现，像"原文没有第一项"）。
    first_line = _first_section_line(lines)
    for index, line in enumerate(lines):
        found = _SECTION_LINE.match(line.strip(" 　\t"))
        if not found and index == first_line:
            found = _FIRST_SECTION_LINE.match(line.strip(" 　\t"))
        if not found:
            continue
        ordinal = _cn_number(found.group(1))
        # 编号回退才是「附件重新开始」（十三之后又出现「一」）；同一编号重复是正常的
        # （这份标书里「二 法定代表人身份证明」和「二 授权委托书」是两个独立格式）。
        if items and ordinal and ordinal < last_ordinal:
            break
        title = found.group(2).strip(" 　\t。，,；;")
        # 目录行被抄进正文时标题后面拖着点线与页码（「一、响应函…………37」），
        # 不洗掉的话大纲里就会出现名叫「响应函…………37」的章 —— 看起来像"第一节不对"，
        # 实际是标题被目录行污染。先切点线、再去页码。
        if _DOT_LEADER.search(title):
            title = _DOT_LEADER.split(title)[0].strip()
        title = _PAGE_TAIL.sub("", title).strip()
        # 标题本身可能很长（「二、法定代表人（单位负责人）身份证明（适用于无委托代理人的
        # 情况）」），不能截短也不能因为长就丢 —— 丢了就是从大纲里整项消失。第一项更长。
        if not title or (len(title) > 120 if index == first_line else len(title) > 60):
            continue
        label = f"{found.group(1)} {title}"
        if label not in items:
            items.append(label)
            last_ordinal = max(last_ordinal, ordinal)
    if not items:
        # 整章都用「（一）（二）…」编号（没有「一、」）的写法：这时整批小节都是
        # **一级**目录项，一个都不能少。只在一条「一、」都没认出来时启用。
        candidates = _subsection_marks(span["body"].splitlines())
        for item in _monotonic_run(candidates):
            label = "%s %s" % (item["ordinal_text"], item["title"])
            if label not in items:
                items.append(label)
    return items


# ══════════════════════════════════════════════════════════════════════════
# 体检判定：原文到底有没有（证据层）
# ══════════════════════════════════════════════════════════════════════════
# 判定主线只有一句：**先问原文有没有，再问抽没抽到**。
#
#   * 抽到了 → 通过（结果本身就是证据，不必再证明原文有）；
#   * 没抽到 → 才去原文找证据：找得到 = 抽取失败（扣分），找不到 = 原文未提供（不扣分）。
#
# 证据一律**独立于抽取器**：抽取器自己的空结果不能同时用来证明「原文没有」，
# 否则扣分永远说得通，也永远无法被证伪。所以证据只扫原文形状：
# 标签行、编号条目行、「由X方自备」句、「格式N」清单、「共 N 台/套」。
#
# 这一层也是「整机台数」不再扣分的依据：原文没有「共 N 台/套」「整机 N 台」这类字样时，
# 它属 not_in_source（原文未提供），只提示、不扣分 —— 但只要有这类字样而没抽到，
# 照样按抽取失败扣分（不是一律放过）。
_MIN_SOURCE_CHARS = 2000
_WS_RUN = re.compile(r"[ \t\u3000]+")
# 全部字段标签（长的在前）：用来识别「这一整行只是标签串、没有取值」。
_ALL_FIELD_LABELS = tuple(sorted({label for labels in FIELD_LABELS.values()
                                  for label in labels}, key=len, reverse=True))
# 像编号的串：与 `looks_like_tender_no` 配合做形态校验（6~40 字、字母数字与 -－—_/）。
_NO_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-－—–_/]{5,40}")


def _clip(text: Any, limit: int = 60) -> str:
    """证据原文：压空白、截到 60 字（界面一行放得下，也不至于把整段抄进来）。"""
    return _WS_RUN.sub(" ", str(text or "")).strip(" 　\t")[:limit]


def _label_value_line(source: str, labels: tuple[str, ...]) -> str:
    """原文里「标签：取值」的第一行 —— 「这个字段原文确实给了」的证据。

    只看形状，不看维度：维度里有没有值是抽取结果，不能拿来证明原文有没有。
    整行只是标签串（「项目名称 项目编号 招标人」这类表格表头）时不算证据。
    """
    for raw in str(source or "").splitlines():
        line = raw.strip(" 　\t")
        if not line or len(line) > 160:
            continue
        flat = _WS_RUN.sub("", line)
        for label in labels:
            at = flat.find(label)
            if at < 0:
                continue
            tail = flat[at + len(label):].lstrip(":：-—–　")
            if len(tail) < 2:
                continue
            if any(tail.startswith(other) for other in _ALL_FIELD_LABELS):
                continue
            return _clip(line)
    return ""


def _tender_no_line(source: str) -> str:
    """原文里「编号别名 + 像编号的值」的第一行 —— 「招标编号原文确实有」的证据。

    别名取自 `FIELD_LABELS["招标编号"]`（实测含「采购编号」「标段编号」这些写法），
    值用 `looks_like_tender_no` 校验形态：光有标签、后面是空的（格式页的
    「采购编号：」）不算证据，光有一串数字没有标签也不算。
    """
    aliases = FIELD_LABELS["招标编号"]
    for raw in str(source or "").splitlines():
        line = raw.strip(" 　\t")
        if not line or len(line) > 160:
            continue
        flat = _WS_RUN.sub("", line)
        if not any(alias in flat for alias in aliases):
            continue
        for match in _NO_TOKEN.finditer(flat):
            if looks_like_tender_no(match.group(0)):
                return _clip(line)
    return ""


def _format_chapter_line(source: str) -> str:
    """原文里「投标文件格式」章的标题行，或成串「格式N 名称」的首条（证据）。"""
    for raw in str(source or "").splitlines():
        line = raw.strip(" 　\t")
        found = _CHAPTER_LINE.match(line)
        if found and any(word in found.group(2) for word in _FORMAT_WORDS):
            return _clip(line)
    items: list[str] = []
    for raw in str(source or "").splitlines():
        line = raw.strip(" 　\t")
        if not _CATALOG_ITEM.match(line):
            continue
        items.append(line)
        if len(items) >= _CATALOG_MIN_ITEMS:
            return _clip(items[0])
    return ""


def _check_item(key: str, kind: str, title: str, detail: str, hint: str, *,
                evidence: str = "", passed: bool = False, level: str = "info",
                gate: bool = False, skipped: bool = False) -> dict[str, Any]:
    """一条考核判定。权重只能来自 `CHECK_WEIGHTS`（不在表里的一律 0 分权重）。"""
    weight, why = CHECK_WEIGHTS.get(key, (0, ""))
    return {"key": key, "kind": kind, "title": title, "detail": detail, "hint": hint,
            "evidence": _clip(evidence), "passed": bool(passed), "level": level,
            "weight": int(weight), "weight_reason": why, "gate": bool(gate),
            "skipped": bool(skipped)}


def _span_label(span: dict[str, Any] | None) -> str:
    return ("%s %s" % (span["label"], span["title"])).strip() if span is not None else ""


def _checkup_facts(source: str, dims: list[dict[str, Any]]) -> dict[str, Any]:
    """原文 + 已抽取维度 → 体检要用的那一份只读事实（抽不到就是空，绝不编造）。"""
    by_name = {str(d.get("name") or ""): d for d in dims or [] if isinstance(d, dict)}
    # 招标编号抽不到时，把原文里「像编号的串」摆出来：人工一眼就能确认是规则没覆盖
    # （例如封面只写「采购编号：HP260901-049」），而不是文件里根本没有编号。
    no_diag = extract_tender_no(source) if source.strip() else {"value": "", "candidates": []}
    no_candidates = [str(x) for x in (no_diag.get("candidates") or []) if str(x).strip()][:8]

    cover: list[dict[str, Any]] = []
    for name, label, required in COVER_FIELDS:
        row = by_name.get(name) or {}
        raw = clean_value(row.get("value")) if row.get("hit") else ""
        ok = bool(raw) and value_usable(name, raw)
        entry = {
            "name": name, "label": label, "required": required,
            "value": raw if ok else "",
            "raw": clean_value(row.get("value")),
            "source": str(row.get("source") or ""),
            "ok": ok,
            "why": "" if ok else ("未抽取到" if not raw else "抽到的值不可用（定义句/列名/残片）"),
        }
        if name == "招标编号" and not ok:
            entry["candidates"] = no_candidates
            if no_candidates:
                entry["why"] = (entry["why"] + "；原文里像编号的串：" + "、".join(no_candidates))[:240]
        cover.append(entry)

    return {
        "no_candidates": no_candidates,
        "cover": cover,
        "items": tech_items(source) if source else [],
        "boundary": boundary_clauses(source) if source else [],
        "blocks": component_blocks(source) if source else [],
        "count": machine_count(source) if source else None,
        "structure": official_structure(source) if source else [],
        "structure_span": format_chapter(source) if source else None,
        "tech_span": tech_chapter(source) if source else None,
        "pipe_rows": len(_PIPE_ROW.findall(source)),
        "space_rows": len(_SPACE_COL_ROW.findall(source)),
        "packages": sorted(set(_PACKAGE.findall(source)))[:8],
        "fills": len(_FILL_BRACKET.findall(source)),
    }


def _outline_covered(structure: list[str], chapters: list[dict[str, Any]]) -> int:
    """当前大纲对得上格式章骨架几项（沿用旧口径：标题前 6 字互相包含）。"""
    titles = [re.sub(r"[\s（）()、，,。《》\"'：:]+", "", str(chapter.get("title") or ""))
              for chapter in chapters or [] if isinstance(chapter, dict)]
    covered = 0
    for name in structure:
        key = re.sub(r"^格式[ \t]*\d+(?:[ \t]*-[ \t]*\d+)?[ \t]*", "", name)
        key = re.sub(r"^[一二三四五六七八九十]{1,3}\s*", "", key)
        key = re.sub(r"[\s（）()、，,。《》\"'：:]+", "", key)[:6]
        if not key:
            continue
        if any(key in title or title[:6] in key for title in titles if title):
            covered += 1
    return covered


def checkup_checks(text: str, dims: list[dict[str, Any]],
                   chapters: list[dict[str, Any]] | None = None,
                   facts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """逐项判定：原文有没有（证据）→ 抽没抽到 → 该不该扣分。

    返回一组判定，每项 ``key/kind/title/detail/hint/evidence/passed/level/weight``：

    * ``kind == extractable`` —— 原文给了、应当抽到；``passed=False`` 表示抽取失败；
    * ``kind == not_in_source`` —— 原文本来没有，只提示、不扣分；
    * ``kind == note`` —— 原文事实（模板填空、引用外部文件…），只提示、不扣分。

    ``facts`` 可由调用方传入（体检主入口已经算过一遍），不传就自己算 —— 两条路径
    判定完全一致，因为判定只依赖原文与已抽取维度。
    """
    source = str(text or "")
    facts = facts if facts is not None else _checkup_facts(source, dims)
    src = source_evidence(source)
    checks: list[dict[str, Any]] = []

    # ── 正文文本：其余每一项的前提 ────────────────────────────────────────
    # 空文本 = 整份文件没进解析（扫描件/图片型 PDF），不是「某一项抽不到」，
    # 所以直接 0 分（gate），不参与分母，也不再逐项报「原文未提供」制造噪音。
    if not source.strip():
        checks.append(_check_item(
            "source_text", KIND_EXTRACTABLE, "没有可解析的正文文本",
            "招标文件文本为空：可能是扫描件/图片型 PDF，或上传解析失败。",
            "重新上传可复制文字的 PDF（或用 OCR 版本）后再解读。",
            evidence="（正文为空）", level="high", gate=True))
        return checks
    if len(source) < _MIN_SOURCE_CHARS:
        checks.append(_check_item(
            "source_text", KIND_EXTRACTABLE, "正文文本过短",
            f"只解析到 {len(source)} 字（正常招标文件 ≥{_MIN_SOURCE_CHARS} 字），"
            "技术要求和评分办法大概率没进来。",
            "确认上传的是完整招标文件；扫描件需先 OCR。",
            evidence=source, level="high"))
    else:
        checks.append(_check_item(
            "source_text", KIND_EXTRACTABLE, "正文文本可用",
            f"解析到 {len(source)} 字，足够逐项核对抽取结果。",
            "无需处理。",
            evidence=source.splitlines()[0] if source.splitlines() else "", passed=True))

    # ── 封面要素：三个必填项各自判「原文有没有」 ──────────────────────────
    cover = {str(row.get("name") or ""): row for row in facts.get("cover") or []}

    row = cover.get("项目名称") or {}
    line = _label_value_line(source, FIELD_LABELS["项目名称"])
    if row.get("ok"):
        checks.append(_check_item(
            "cover_name", KIND_EXTRACTABLE, "封面要素可用·招标项目名称",
            "原文有项目名称，维度里也取到了可用值。", "无需处理。",
            evidence=line or str(row.get("value") or ""), passed=True))
    elif line:
        checks.append(_check_item(
            "cover_name", KIND_EXTRACTABLE, "封面要素缺失或不可用·缺招标项目名称",
            f"原文有「{_clip(line, 30)}」，但维度里的项目名称不可用"
            f"（{row.get('why') or '未抽取到'}）：封面的项目名称会退化成平台项目名。",
            "在「招标解读 → 维度」里人工填一次，人工值不会被规则覆盖。",
            evidence=line, level="high"))
    else:
        checks.append(_check_item(
            "cover_name", KIND_NOT_IN_SOURCE, "封面要素·原文未提供招标项目名称",
            "原文里没有「项目名称：」这类带取值的标签行，这一项按「原文未提供」处理，不扣分。",
            "若原文其实写了（只是没有标签），在「招标解读 → 维度」里人工填一次。"))

    row = cover.get("招标编号") or {}
    line = _tender_no_line(source)
    if row.get("ok"):
        checks.append(_check_item(
            "cover_no", KIND_EXTRACTABLE, "封面要素可用·招标编号",
            "原文有编号、维度里也取到了可用值。", "无需处理。",
            evidence=line or str(row.get("value") or ""), passed=True))
    elif line:
        detail = (f"原文这一行有编号（「{_clip(line, 30)}」），但维度里没取到可用值"
                  f"（{row.get('why') or '未抽取到'}）：封面编号会留空，投标函与各份格式文书都受影响。")
        if facts.get("no_candidates"):
            detail += "原文里像编号的串：" + "、".join(facts["no_candidates"]) + "。"
        checks.append(_check_item(
            "cover_no", KIND_EXTRACTABLE, "封面要素缺失或不可用·缺招标编号",
            detail, "在「招标解读 → 维度」里人工填一次，或确认编号别名后重解析。",
            evidence=line, level="warn"))
    else:
        checks.append(_check_item(
            "cover_no", KIND_NOT_IN_SOURCE, "封面要素·原文未提供招标编号",
            "原文没有「招标编号/采购编号/文件编号…」+ 像编号的取值，"
            "这一项按「原文未提供」处理，不扣分。",
            "若原文其实有编号（只是标签写法不同），把那行发我，我补别名。"))

    row = cover.get("招标人") or {}
    line = _label_value_line(source, FIELD_LABELS["招标人"])
    if row.get("ok"):
        checks.append(_check_item(
            "cover_owner", KIND_EXTRACTABLE, "封面要素可用·招标人",
            "原文有招标人/采购人，维度里也取到了可用值。", "无需处理。",
            evidence=line or str(row.get("value") or ""), passed=True))
    elif line:
        checks.append(_check_item(
            "cover_owner", KIND_EXTRACTABLE, "封面要素缺失或不可用·缺招标人",
            f"原文有「{_clip(line, 30)}」，但维度里的招标人不可用"
            f"（{row.get('why') or '未抽取到'}）：投标函与质疑函的抬头上会是空的。",
            "在「招标解读 → 维度」里人工填一次。",
            evidence=line, level="warn"))
    else:
        checks.append(_check_item(
            "cover_owner", KIND_NOT_IN_SOURCE, "封面要素·原文未提供招标人",
            "原文里没有「招标人/采购人/采购单位」这类带取值的标签行，"
            "这一项按「原文未提供」处理，不扣分。",
            "若原文其实写了（只是没有标签），在「招标解读 → 维度」里人工填一次。"))

    # ── 技术要求条目 ──────────────────────────────────────────────────────
    items = facts.get("items") or []
    item_count = len(items)
    sample = " / ".join(src["numbered_sample"][:2])
    if item_count >= 3:
        checks.append(_check_item(
            "tech_items", KIND_EXTRACTABLE, "技术要求条目已抽到",
            f"从原文技术小节抽到 {item_count} 条编号条目，「逐条响应」与对照表行数都有依据。",
            "无需处理。",
            evidence=sample or src["tech_heading"], passed=True))
    elif src["numbered_items"] >= 3:
        # 证据：技术小节里确实有 ≥3 条编号条目（与抽取器无关的原文形状），但抽取器返回得更少。
        checks.append(_check_item(
            "tech_items", KIND_EXTRACTABLE, "技术要求条目几乎没抽到",
            f"原文技术小节里有 {src['numbered_items']} 条编号条目（如「{sample}」），"
            f"抽取器只返回 {item_count} 条：逐条响应和对照表行数都会缺。",
            "换一种编号写法（表格/散段落）时先把技术要求贴成「1、2、3」条目，或人工在维度里补。",
            evidence=sample, level="high"))
    else:
        checks.append(_check_item(
            "tech_items", KIND_NOT_IN_SOURCE, "技术要求条目·原文未提供",
            f"原文里没有成形的「技术要求」编号条目（技术小节里识别到 "
            f"{src['numbered_items']} 条编号段落），这一项按「原文未提供」处理，不扣分。",
            "若原文其实有条目（只是编号写法不同），把那一节前几行发我，我补规则。",
            evidence=src["tech_heading"]))

    # ── 部件级参数块 ──────────────────────────────────────────────────────
    blocks = facts.get("blocks") or []
    if blocks:
        checks.append(_check_item(
            "tech_params", KIND_EXTRACTABLE, "部件参数块已抽到",
            f"从技术要求条目里抽到 {len(blocks)} 个部件参数块。", "无需处理。",
            evidence="、".join(str(block.get("name") or "") for block in blocks[:3]),
            passed=True))
    elif src["param_blocks"] >= 1:
        checks.append(_check_item(
            "tech_params", KIND_EXTRACTABLE, "有技术要求条目，但没有部件级参数块",
            f"原文有 {src['param_blocks']} 个成组「参数名：值」的参数块"
            f"（如「{(src['param_sample'] or [''])[0]}」），但一个都没抽出来："
            "参数对照表容易只写一两个词。",
            "重点抽查「参数对照表」「技术参数」章节，看参数是否写全。",
            evidence=(src["param_sample"] or [""])[0], level="warn"))
    else:
        checks.append(_check_item(
            "tech_params", KIND_NOT_IN_SOURCE, "部件级参数块·原文未提供",
            "原文技术要求里没有成组的「参数名：值」块，这一项按「原文未提供」处理，不扣分。",
            "若原文其实有（写在表格里），把那一节发我，我补表格取值。"))

    # ── 供货边界条款 ──────────────────────────────────────────────────────
    boundary = facts.get("boundary") or []
    if boundary:
        checks.append(_check_item(
            "boundary", KIND_EXTRACTABLE, "供货边界条款已抽到",
            f"抽到 {len(boundary)} 条「由X方自备/自行配套」的边界条款。", "无需处理。",
            evidence=" / ".join(_clip(item["text"], 24) for item in boundary[:2]),
            passed=True))
    elif src["boundary_line"]:
        checks.append(_check_item(
            "boundary", KIND_EXTRACTABLE, "没有识别到供货边界条款",
            f"原文有「{_clip(src['boundary_line'], 30)}」这类句子，但没被当成边界条款："
            "供货范围可能把矿方自备件写进去。",
            "复核技术要求里的「由X方自备/自行配套」条款，必要时人工在维度里标注。",
            evidence=src["boundary_line"], level="warn"))
    else:
        checks.append(_check_item(
            "boundary", KIND_NOT_IN_SOURCE, "供货边界条款·原文未提供",
            "原文里没有「由X方自备/自行配套/不在供货范围」这类句子，"
            "这一项按「原文未提供」处理，不扣分。",
            "若原文有类似句子但没被识别，把原句发我，我补规则。"))

    # ── 整机台数 ──────────────────────────────────────────────────────────
    target_count = facts.get("count")
    if target_count:
        checks.append(_check_item(
            "machine_count", KIND_EXTRACTABLE, "整机台数已抽到",
            f"从原文抽到整机台数 {target_count}。", "无需处理。",
            evidence=src["machine_count_line"], passed=True))
    elif src["machine_count_line"]:
        checks.append(_check_item(
            "machine_count", KIND_EXTRACTABLE, "没识别到整机台数",
            f"原文写了数量（「{_clip(src['machine_count_line'], 30)}」），但没抽到整机台数："
            "单台配置与项目总量无法交叉核对，数量口径可能自相矛盾。",
            "在维度里确认数量，或让正文写「随主机配套」而不是自填数字。",
            evidence=src["machine_count_line"], level="warn"))
    else:
        checks.append(_check_item(
            "machine_count", KIND_NOT_IN_SOURCE, "整机台数·原文未提供",
            "原文没有「共 N 台/套」「整机 N 台」这类整机数量口径，"
            "这一项按「原文未提供」处理，不扣分；单台配置与项目总量也无需交叉核对。",
            "若原文在别处写了数量（供货清单/表格），把那一段发我，我补规则。"))

    # ── 投标文件格式章骨架 ────────────────────────────────────────────────
    structure = facts.get("structure") or []
    line = _format_chapter_line(source)
    if structure:
        checks.append(_check_item(
            "format_structure", KIND_EXTRACTABLE, "投标文件格式章骨架已抽到",
            f"格式章抽出 {len(structure)} 项，成稿大纲可以按它对齐。", "无需处理。",
            evidence=" · ".join(structure[:3]), passed=True))
    elif line:
        checks.append(_check_item(
            "format_structure", KIND_EXTRACTABLE, "「投标文件格式」章没抽出章节骨架",
            f"原文有格式章（「{_clip(line, 30)}」），但一项章节骨架都没抽出来："
            "成稿里的法定文书只能靠模型自写。",
            "在「结构模板」里选「按招标文件格式（自动）」重建大纲，或手工补齐该章的格式条目。",
            evidence=line, level="warn"))
    else:
        checks.append(_check_item(
            "format_structure", KIND_NOT_IN_SOURCE, "投标文件格式章·原文未提供",
            "这份文件里没有规定投标文件格式的章（标题不含「投标文件格式 / 响应文件格式 / 文件格式」"
            "这类字样，也没有成串的「格式N」清单），这一项按「原文未提供」处理，不扣分。",
            "若该章标题写法不同，把标题发我补规则；否则先用其他模板生成，再按招标文件人工调整大纲。"))

    # ── 大纲与格式章骨架是否对得上 ────────────────────────────────────────
    if not structure:
        checks.append(_check_item(
            "outline_match", KIND_NOTE, "大纲核对未执行",
            "这份文件没有可用的格式章骨架，无法与当前大纲比对。",
            "无需处理。", skipped=True))
    elif not chapters:
        checks.append(_check_item(
            "outline_match", KIND_NOTE, "大纲核对未执行",
            "还没有生成章节，本次不核对大纲与格式章的对应关系。",
            "先生成大纲，再回来体检。", skipped=True))
    else:
        covered = _outline_covered(structure, chapters)
        need = max(3, len(structure) // 2)
        chapter_name = _span_label(facts.get("structure_span")) or "投标文件格式"
        if covered >= need:
            checks.append(_check_item(
                "outline_match", KIND_EXTRACTABLE, "大纲与格式章骨架对得上",
                f"格式章 {len(structure)} 项里，当前大纲覆盖了 {covered} 项。", "无需处理。",
                passed=True))
        else:
            checks.append(_check_item(
                "outline_match", KIND_EXTRACTABLE,
                "大纲与「%s」对不上" % chapter_name,
                f"招标文件{chapter_name}要求 {len(structure)} 个部分，"
                f"当前大纲只对得上 {covered} 个。",
                "在「结构模板」里选「按招标文件格式（自动）」重建大纲，"
                "或按该章顺序手工调整章节标题。",
                evidence=" · ".join(structure[:3]), level="warn"))

    # ── 原文事实（只提示，不扣分） ────────────────────────────────────────
    external = _EXTERNAL_REF.search(source)
    if external:
        checks.append(_check_item(
            "external_ref", KIND_NOTE, "参数可能引用外部文件",
            f"原文出现「{external.group(0)[:30]}」，本文件可能不含完整技术参数。",
            "把技术协议/技术规范书一并上传，否则参数章节只能写空话。",
            evidence=external.group(0)))
    if len(facts.get("packages") or []) >= 2:
        checks.append(_check_item(
            "packages", KIND_NOTE, "多标段/多包件",
            "识别到标段：" + "、".join(facts["packages"]) + "。",
            "数量与交付期要按标段分别核对，别写成一个总数。",
            evidence="、".join(facts["packages"])))
    if facts.get("fills"):
        fill = _FILL_BRACKET.search(source)
        checks.append(_check_item(
            "template_fills", KIND_NOTE,
            f"原文含 {facts['fills']} 处模板填空（【一】【　】）",
            "这些是合同模板的空格，不是招标人给的承诺值。",
            "导出时会自动转成红色待填，出件前必须逐项填掉。",
            evidence=fill.group(0) if fill else ""))
    table_rows = int(facts.get("pipe_rows") or 0) + int(facts.get("space_rows") or 0)
    if table_rows >= 20:
        first_row = _PIPE_ROW.search(source) or _SPACE_COL_ROW.search(source)
        checks.append(_check_item(
            "table_rows", KIND_NOTE, "表格可能是文本化排版",
            f"识别到约 {table_rows} 行「管道符/多空格分列」的行，表格解析稳定性会下降。",
            "抽查「参数对照表」的列是否错位；错位时把该章重新生成一次。",
            evidence=first_row.group(0) if first_row else ""))
    return checks


def _risks_from_checks(checks: list[dict[str, Any]],
                       deductions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """判定 → 界面风险条（字段名一个不动：level / title / detail / hint）。

    * ``extractable`` 未通过 → 按严重度（high / warn）出现，说明里带上「扣 N 分」；
    * ``not_in_source`` / ``note`` → 一律 info（提示），说明里写明不扣分，
      界面上的「必须处理 / 建议核对」数量因此不会被「原文本来没写」虚增；
    * ``skipped`` 的项（还没有大纲）不进风险条，免得每次体检都挂一条噪音。
    """
    points = {str(item.get("key") or ""): int(item.get("points") or 0)
              for item in deductions}
    risks: list[dict[str, str]] = []
    for item in checks:
        if item.get("skipped"):
            continue
        if item.get("kind") == KIND_EXTRACTABLE:
            if item.get("passed"):
                continue
            detail = str(item.get("detail") or "")
            if points.get(str(item.get("key") or "")):
                detail = "%s（扣 %d 分）" % (detail, points[str(item.get("key") or "")])
            risks.append(_risk(str(item.get("level") or "warn"), str(item.get("title") or ""),
                               detail, str(item.get("hint") or "")))
            continue
        risks.append(_risk("info", str(item.get("title") or ""),
                           str(item.get("detail") or ""), str(item.get("hint") or "")))
    return risks


def build_checkup(text: str, dims: list[dict[str, Any]],
                  chapters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """体检主入口：只读原文与已抽取维度，不调用大模型、不写数据库。

    打分口径（详见 `checkup_score`）：只有「原文确实给了、却没抽到」的项才扣分；
    原文没给的（not_in_source）与原文事实（note）只提示。分母是本次可抽取项的权重和，
    所以「原文没提供整机台数」不会再拉低分数。

    返回结构（老字段一个不动，新字段都可选，老前端不读也不报错）：

    * ``score`` / ``max_score`` / ``label`` —— 分数与等级；
    * ``deductions[]`` —— 每项：``key/title/points``（扣了多少）``/reason``（为什么）
      ``/evidence``（原文，≤60 字）``/level/share``（未截断前的应扣）``/capped``；
    * ``must_fix[]`` / ``review[]`` —— 上面那批里「必须处理 / 建议核对」的子集：
      只有 ``extractable`` 且等级够高的项才进来，``not_in_source`` 永不出现；
    * ``not_in_source[]`` —— 原文本来没有、因此不扣分的项及说明；
    * ``notes[]`` —— 原文事实（模板填空、引用外部文件…），不扣分；
    * ``skipped[]`` —— 这次没有条件的核对（还没有大纲），不扣分；
    * ``evidence`` —— 分母怎么算的：公式、分母、每项权重与理由、截断情况；
    * ``risks`` / ``stats`` / ``cover`` / ``tech_items`` / … —— 老字段原样保留。
    """
    source = str(text or "")
    facts = _checkup_facts(source, dims)
    stats = {
        "chars": len(source),
        "lines": len([x for x in source.splitlines() if x.strip()]),
        "tech_items": len(facts["items"]),
        "boundary": len(facts["boundary"]),
        "components": len(facts["blocks"]),
        "machine_count": facts["count"] or 0,
        "official_chapters": len(facts["structure"]),
        "table_like_rows": facts["pipe_rows"] + facts["space_rows"],
    }
    checks = checkup_checks(source, dims, chapters, facts=facts)
    scored = score_checkup(checks)
    score = int(scored["score"])
    label = "良好" if score >= 85 else ("需要注意" if score >= 60 else "风险较高")
    risks = _risks_from_checks(checks, scored["deductions"])

    actions: list[str] = []
    for risk in risks:
        if risk["hint"] not in actions:
            actions.append(risk["hint"])

    return {
        "score": score,
        "max_score": int(scored["max_score"]),
        "label": label,
        "risks": risks,
        "deductions": scored["deductions"],
        "must_fix": scored["must_fix"],
        "review": scored["review"],
        "not_in_source": scored["not_in_source"],
        "notes": scored["notes"],
        "skipped": scored["skipped"],
        "evidence": scored["evidence"],
        "actions": actions[:6],
        "stats": stats,
        "cover": facts["cover"],
        "tech_items": [{"index": item["index"], "head": item["head"],
                        "boundary": item["boundary"], "text": item["text"][:120]}
                       for item in facts["items"]],
        "boundary": [{"index": c["index"], "text": c["text"][:160],
                      "items": c["items"], "qualifier": c["qualifier"]}
                     for c in facts["boundary"]],
        "components": [{"name": b["name"],
                        "label": b["name"] if not _NOT_COMPONENT.search(b["name"])
                                 else b["name"] + "（整机/系统参数块）",
                        "is_component": not _NOT_COMPONENT.search(b["name"]),
                        "params": dict(list(b["params"].items())[:16]),
                        "count": len(b["params"])} for b in facts["blocks"]],
        "quantities": {"machine_count": facts["count"],
                       "packages": facts["packages"]},
        "structure": facts["structure"],
        "structure_chapter": _span_label(facts["structure_span"]),
        "tech_chapter": _span_label(facts["tech_span"]),
        "chapter_count": len(chapters or []),
    }
