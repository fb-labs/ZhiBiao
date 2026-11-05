"""结构化表格渲染层：把 `pdf_tables` 的网格**直出**成 Markdown，交给现有 Word 管线。

为什么单独一层：`format_forms` 现在的做法是"按两个空格猜列"，猜错了只能再加规则
（三轮修复都栽在这里）。有了行列网格就不再猜——列数、合并、合计位置、注释归属
全是结构里带出来的事实。本模块只做"结构 → 文本"，不做任何版面推断。

出口仍然是 Markdown 表格：`pipeline_api._md_render_chapter` 会把连续 `|` 行交给
`_md_add_table` 变成真 Word 表格（`Table Grid` 全框线、表头重复、列宽按内容加权）。
所以这里必须保证**每一行列数一致**——`_md_normalize_rows` 是以第一行定宽、
多出来的格子会被并进最后一列（会串行），不能靠它兜底。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from zhibiao.core.pdf_tables import classify_line_role

# 表外文字排位：说明/注释在前，落款在最后；同组内保持原文顺序
_TAIL_RANK = {"body": 0, "note": 0, "placeholder": 0, "signature": 1}

_PIPE = re.compile(r"\|")
_WS = re.compile(r"[\t\r\n]+")


class _Grid:
    """把一行的单元格按 Markdown 需要铺开。

    `rows` 来自 `pdf_tables`：合并单元格的文字只在左上格，被覆盖的格是 ""。
    """

    def __init__(self, table: Mapping[str, Any]) -> None:
        self.table = table
        self.rows: list[list[str]] = [list(row) for row in table.get("rows") or []]
        self.header_rows = int(table.get("header_rows") or 0)
        self.n_cols = len(self.rows[0]) if self.rows else 0
        self.cells = list(table.get("cells") or [])

    # -- 单格文本 ---------------------------------------------------------
    @staticmethod
    def _clean(text: Any) -> str:
        """竖线会拆坏 Markdown 表格；换行会把一行拆成两行。"""
        return _PIPE.sub("｜", _WS.sub(" ", str(text or ""))).strip()

    def row_role(self, index: int) -> str:
        """整行角色 = 该行左上角单元格的角色（合计/占位）。"""
        for cell in self.cells:
            if int(cell.get("row", -1)) == index and int(cell.get("col", -1)) == 0:
                return str(cell.get("role") or "data")
        return "data"

    # -- 行序 -------------------------------------------------------------
    def ordered_rows(self) -> list[int]:
        """表体行序：合计行强制排到最后（同一类保持原顺序）。

        PDF 里合计偶尔夹在中间（比如分页后重复表头再接合计），Word 里必须收尾，
        否则"合计"后面又冒出数据行，评委看到的是错的加总关系。
        """
        body = list(range(self.header_rows, len(self.rows)))
        normal = [i for i in body if self.row_role(i) != "total"]
        total = [i for i in body if self.row_role(i) == "total"]
        return list(range(self.header_rows)) + normal + total

    def header(self) -> list[list[str]]:
        """表头：colspan 展开成重复列名（`序号` 跨两列 → `| 序号 | 序号 |`）。"""
        out: list[list[str]] = []
        for index in range(self.header_rows):
            line = [self._clean(value) for value in self.rows[index]]
            for cell in self.cells:
                if int(cell.get("row", -1)) != index:
                    continue
                span = int(cell.get("colspan") or 1)
                col = int(cell.get("col", 0))
                if span > 1:
                    text = self._clean(cell.get("text"))
                    for offset in range(1, span):
                        if col + offset < len(line):
                            line[col + offset] = text
            out.append(line)
        return out

    def body_line(self, index: int) -> list[str]:
        """数据行：被 colspan 覆盖的格子留空（与原文一致，不复制文字）。"""
        return [self._clean(value) for value in self.rows[index]]

    def is_label_value_table(self) -> bool:
        """真正的两列"标签 / 值"表：没有表头行，且网格本身就是两列。

        判定为什么不用"每行最多两个非空格"：实测资格（一）基本情况表是 **5 列**的
        标签/值表（`注册资金 | 值 | 成立时间 | 值`，`联系方式` 还纵向跨两行），
        按"标签+右邻格"配对会把 `联系方式 | 联系人` 这种**两个标签**配成一对，
        得到的是"标签 = 标签"，比原文更糟。所以只有网格本来就是两列时才折成两列；
        其余情况照网格出（见 `render_structured_table` 里"不编造表头"的处理）。
        """
        return self.header_rows == 0 and self.n_cols == 2


def _md_line(values: Sequence[str], width: int) -> str:
    padded = list(values[:width]) + [""] * max(0, width - len(values))
    return "| " + " | ".join(padded[:width]) + " |"


def _md_separator(width: int) -> str:
    return "| " + " | ".join(["---"] * width) + " |"


def _label_value_lines(grid: _Grid) -> list[str]:
    """竖排标签/值表（`header_rows == 0`）：两列直出，**不伪造表头**。

    资格（一）基本情况表这类表没有列名行，硬套一个 `| 项目 | 内容 |` 表头
    等于凭空加一行原文没有的字。这里按"标签 + 右邻格值"配对，一对一行：
        | 投标人名称 |        |
        | 注册资金   |        |
        | 成立时间   |        |
    行内有两组标签/值（`注册资金 | 值 | 成立时间 | 值`）时按顺序拆成两行，
    内容一个字不丢，只是把左右并排改成上下排列 —— 这是竖排表本来的读法。
    """
    lines: list[str] = []
    for index in range(len(grid.rows)):
        row_cells = sorted((cell for cell in grid.cells if int(cell.get("row", -1)) == index),
                           key=lambda cell: int(cell.get("col", 0)))
        # 同一行里可能有两组（标签, 值），逐对输出；一格里没字也照样占位，
        # 保证每行列数恒为 2（_md_normalize_rows 以首行定宽，串行会毁表）。
        for pos in range(0, len(row_cells), 2):
            label = grid._clean(row_cells[pos].get("text"))
            value = grid._clean(row_cells[pos + 1].get("text")) if pos + 1 < len(row_cells) else ""
            if not label and not value:
                continue
            lines.append(_md_line([label, value], 2))
    return lines


def render_structured_table(table: Mapping[str, Any]) -> list[str]:
    """一张结构化表 → Markdown 行（第一行是表头，第二行是分隔行）。

    返回空列表表示这张表没法渲染（没有网格），调用方应回退到文本启发式。
    """
    grid = _Grid(table)
    if not grid.rows or grid.n_cols < 1:
        return []

    if grid.is_label_value_table():
        pairs = _label_value_lines(grid)
        if not pairs:
            return []
        # Markdown 语法上第一行就是表头；放**真实内容**而不是编造的"项目/内容"，
        # Word 端只会把第一行加粗，不会多出一行原文没有的字。
        return [pairs[0], _md_separator(2)] + pairs[1:]

    width = grid.n_cols
    body_order = grid.ordered_rows()[grid.header_rows:]
    header_lines = grid.header()
    if not header_lines:
        # 没有表头行（资格（一）这类竖排表）：Markdown 语法上表头行必须存在，
        # 那就拿**原文真实的第一行**顶上，绝不编造「项目 / 内容」这种原文没有的字。
        if not body_order:
            return []
        header_lines = [grid.body_line(body_order.pop(0))]
    lines = [_md_line(row, width) for row in header_lines]
    lines.append(_md_separator(width))
    for index in body_order:
        lines.append(_md_line(grid.body_line(index), width))
    return lines


def order_context_lines(lines: Iterable[str]) -> list[str]:
    """表外文字排位：说明/注释在前，落款在最后；同组内保持原文顺序。

    原文里落款可能排在注释之前（PDF 里就是那个位置），但在标书里
    "注：…" 必须紧跟表格、落款收尾，否则读起来像把签字页插进了说明中间。
    """
    ranked: list[tuple[int, int, str]] = []
    for position, text in enumerate(lines):
        clean = str(text or "").strip()
        if not clean:
            continue
        ranked.append((_TAIL_RANK.get(classify_line_role(clean), 0), position, clean))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def render_structured_section(tables: Sequence[Mapping[str, Any]], *,
                              title: str = "") -> str:
    """一节的若干张表 → 正文 Markdown。

    每张表按"表前文字 → 表格 → 表后文字"输出，文字按 `order_context_lines` 排位。
    相邻两张表之间重复的文字只保留一次（同一句注释很容易被前表的 lines_after
    和后表的 lines_before 同时收进来）。
    """
    parts: list[str] = []
    seen: set[str] = set()
    for table in tables:
        before = order_context_lines(table.get("lines_before") or [])
        rendered = render_structured_table(table)
        if not rendered:
            continue
        if before or parts:
            fresh = [line for line in before if line not in seen]
            if fresh:
                parts.append("\n\n".join(fresh))
                seen.update(fresh)
        parts.append("\n".join(rendered))
        after = order_context_lines(table.get("lines_after") or [])
        fresh_after = [line for line in after if line not in seen]
        if fresh_after:
            parts.append("\n\n".join(fresh_after))
            seen.update(fresh_after)
    return "\n\n".join(part for part in parts if part.strip()).strip()


def _squash(text: Any) -> str:
    """比较用：去掉所有空白与全角空格，避免 PDF 断行/空格把标题切碎。"""
    return re.sub(r"[\s\u3000]+", "", str(text or ""))


# ══════════════════════════════════════════════════════════════════════════
# 结构化出稿 vs 启发式出稿：信息量对比 + 行级合并（结构性保险）
# ══════════════════════════════════════════════════════════════════════════
#
# 为什么需要这一层（线上风险，不是个例）：
# `render_structured_section` 是**按"检出几张网格"组织正文**的，不是按"这一节原文有
# 哪些字"组织正文——它只输出 pdfplumber 检出网格的表 + 表前后紧邻的几行。于是任何
# "有文字、没表格"的节（「九、投标设备技术性能指标的详细描述」「十三、其他资料」这类
# 纯文字小节）在结构化路径下会被掏空：一个网格都没有 → 表外文字也没被带出来 → 整段
# 正文消失。ensure_format_subsections 只补得上"（一）~（八）"那种**同级小节**，
# 补不上"任意一节的正文"，所以保险必须做在渲染出口上。
#
# 口径：
# 1. **成稿的信息量不允许比启发式更少**：结构化出稿后必须与 `render_section_body`
#    （一行未改的旧路径）比"文字有没有丢"；
# 2. 变薄时**不是二选一，而是行级合并**（下面 `merge_structured_with_heuristic`）：
#    表格块以结构化为准（列数、合并、合计位置、注释归属来自版式，启发式是照着两个
#    空格猜列，历史错位全栽在这里），表外正文以原文为准（启发式里"表格没覆盖到的
#    文字行"按原顺序补回来，表格块作为锚点留在它在原文里的位置）。
#
# 为什么判据不是"整体字数 < 0.6 倍"这一个式子（需求给的是这个量纲，但单独用它会在
# 既有的「八、资格审查资料」上误判）：那一节原文是八个小节，pdfplumber 只检出四张
# 网格，实测结构化 225 字 / 启发式 862 字 = 0.26 —— 这种"少"不是丢信息，少掉的是
# （二）（五）（六）（八）的**小节标题 + 一行注释**，它们由
# `format_subsections.ensure_format_subsections` 按原文清单补法定框架（财务状况表的
# 年度表头、审计报告图框、三网查询截图渠道）。把启发式那几行生文本并进来，补全逻辑
# 会认为"小节已存在且有内容"，把法定框架整块跳过 —— 那才是真的丢分项。
# 所以判据落在**有没有成段正文整段丢失**上（一个小节标题 / 栏目名 / 落款都不是
# "成段文字"），比例口径只在"这一节的正文不属于（一）（二）这种小节"时才作为补充。

# 一行文字平掉空白后到 40 字，才像"一段正文"；小节标题（（二）近年财务状况表）、
# 栏目名（投标人名称 注册地址）、落款（投标人：（盖单位章））都短于这个长度。
_PROSE_SENTENCE = 40
# 启发式里丢掉的成段文字累计到 60 字（≈ 一句半）才算"正文被掏空"
_PROSE_LOST = 60
# 需求里的比例口径：结构化正文（不含表格）< 启发式正文 × 0.6
_PROSE_RATIO = 0.6
# 启发式正文本身不足 100 字时没有"信息量"可比（短节只够一行字，比比例没意义）
_PROSE_MIN = 100
# 表格对应关系：启发式猜的表里有这么多格子能在某张结构化表里找到，就是同一张表
_TABLE_OVERLAP = 0.5
# 「（一）」「（三）」这类小节标题：出现它们说明这一节的缺失由小节补全负责
_SUBSECTION_HEADING = re.compile(r"^[（(][一二三四五六七八九十百零〇\d]{1,3}[）)]")


def _blocks(text: Any) -> list[str]:
    """正文 → 块（与 `format_forms` 一致：块之间是空行）。"""
    return [part for part in re.split(r"\n{2,}", str(text or "")) if part.strip()]


def _is_table_block(block: str) -> bool:
    """整块都是 `|` 行 = 一张 Markdown 表格块（Word 端把连续 `|` 行画成一张表）。"""
    rows = [line for line in str(block or "").splitlines() if line.strip()]
    return bool(rows) and all(line.lstrip().startswith("|") for line in rows)


def _table_cells(block: str) -> list[str]:
    """表格块里的格子文字（`| --- |` 分隔行不算）。"""
    cells: list[str] = []
    for line in str(block or "").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for cell in line.strip().strip("|").split("|"):
            text = cell.strip()
            if text and not set(text) <= {"-", ":", " "}:
                cells.append(text)
    return cells


def _text_lines(text: Any) -> list[str]:
    """正文 → 表外文字行（按原顺序，不含表格行）。"""
    lines: list[str] = []
    for block in _blocks(text):
        if _is_table_block(block):
            continue
        lines.extend(line.strip() for line in block.splitlines() if line.strip())
    return lines


def _text_weight(text: Any) -> int:
    """正文字数（去空白、不算表格）：表格归结构化层管，这里只比"文字丢没丢"。"""
    return sum(len(_squash(line)) for line in _text_lines(text))


def missing_text_lines(structured: str, heuristic: str) -> list[str]:
    """启发式里有、结构化里找不到的文字行（按启发式的原顺序）。

    比对用 `_squash`（去全部空白）而不是逐字相等：启发式会把 PDF 折行接回去
    （`…投标总报价，` + `工期　　…` 变成一行），而结构化只带表前后紧邻的几行，
    两边断行不同是常态，按字面比会把同一句话当成两句话。
    """
    haystack = _squash(structured)
    missing: list[str] = []
    for line in _text_lines(heuristic):
        key = _squash(line)
        if not key or key in haystack:
            continue
        # 已经被更长的缺失行覆盖（同一句话被启发式断成两行）→ 不再重复记
        if any(key in _squash(other) for other in missing):
            continue
        missing.append(line)
    return missing


def is_structured_thinner_than_heuristic(structured: str, heuristic: str) -> bool:
    """结构化结果是不是**把启发式里的成段正文掏空了**；是则返回 True。

    两道判据（按顺序）：
    1. 成段文字丢失：启发式里 ≥`_PROSE_SENTENCE` 字的正文行在结构化里找不到，
       累计 ≥`_PROSE_LOST` 字 —— 这就是「九、投标设备技术性能指标的详细描述」的现场；
    2. 比例口径（需求里的 `< 0.6`）：只在"这一节的正文不属于（一）（二）这种小节"
       且启发式正文 ≥`_PROSE_MIN` 字时才用 —— 小节型的节缺失由
       `ensure_format_subsections` 补法定框架，合并生文本反而会把框架跳过（见本节
       开头"为什么判据不是整体字数"）。
    """
    if not str(structured or "").strip() or not _text_weight(heuristic):
        return False
    lost = sum(len(_squash(line)) for line in missing_text_lines(structured, heuristic)
               if len(_squash(line)) >= _PROSE_SENTENCE)
    if lost >= _PROSE_LOST:
        return True
    if any(_SUBSECTION_HEADING.match(line) for line in _text_lines(heuristic)):
        return False
    if _text_weight(heuristic) < _PROSE_MIN:
        return False
    return _text_weight(structured) < _text_weight(heuristic) * _PROSE_RATIO


def _table_overlap(block: str, candidate: str) -> float:
    """启发式猜的一张表与某张结构化表重合多少（格子文字被包含的比例，0~1）。"""
    cells = [cell for cell in _table_cells(block) if len(_squash(cell)) >= 2]
    if not cells:
        return 0.0
    table = _squash(candidate)
    return sum(1 for cell in cells if _squash(cell) in table) / len(cells)


def merge_structured_with_heuristic(structured: str, heuristic: str) -> str:
    """行级合并：结构化画出的表格 + 启发式里"表格没覆盖到"的文字行，按原文顺序。

    合并规则（三条，都不删内容）：
    - 表格块以**结构化**为准：结构化有对应表（格子文字重合过半）时丢掉启发式猜的
      那张（猜列会串行、会少列，历史错位就是这么来的），并把插入指针推到那张表之后
      —— 表后面的注释仍然落在表后面，不会跑到表头上去；
    - 启发式猜出来、结构化**完全没有对应表**的表照样保留：宁可多一张表，也不许因为
      pdfplumber 漏检把整张表连同里面的数字丢掉；
    - 文字行只在"结构化里找不到"时补：按启发式的顺序插到**它后面最近一个锚点块之前**，
      锚点找不到就追加到末尾 —— 表格块因此仍然留在它在原文里的位置。

    返回合并后的正文；返回空串表示合并不出来（调用方应整节退回启发式）。
    """
    s_blocks = _blocks(structured)
    if not s_blocks:
        return ""
    s_flat = _squash(structured)
    block_flat = [_squash(block) for block in s_blocks]

    insert: dict[int, list[str]] = {}
    appended: list[str] = []
    cursor = 0

    def before(position: int, extra: str) -> None:
        insert.setdefault(min(max(position, 0), len(s_blocks)), []).append(extra)

    for block in _blocks(heuristic):
        if _is_table_block(block):
            best, score = None, 0.0
            for index in range(cursor, len(s_blocks)):
                if not _is_table_block(s_blocks[index]):
                    continue
                overlap = _table_overlap(block, s_blocks[index])
                if overlap > score:
                    best, score = index, overlap
            if best is not None and score >= _TABLE_OVERLAP:
                cursor = best + 1        # 结构化有这张表：指针越过它，后面的字接在表后
            else:
                before(cursor, block.strip())
            continue
        key = _squash(block)
        if not key:
            continue
        # 锚点：结构化的某一块里已经包含了这段文字 → 指针推过去，不补
        match = next((i for i in range(cursor, len(s_blocks)) if key in block_flat[i]), None)
        if match is not None:
            cursor = match + 1
            continue
        if key in s_flat:
            continue
        fresh: list[str] = []
        for line in block.splitlines():
            text = _squash(line)
            if not text or text in s_flat or any(text in old for old in appended):
                continue
            fresh.append(line.strip())
            appended.append(text)
        if fresh:
            before(cursor, "\n".join(fresh))

    merged: list[str] = []
    for index, block in enumerate(s_blocks):
        merged.extend(insert.get(index, []))
        merged.append(block.strip())
    merged.extend(insert.get(len(s_blocks), []))
    return "\n\n".join(part for part in merged if part.strip()).strip()


# ══════════════════════════════════════════════════════════════════════════
# 节 ↔ 页：不写死页码，按标题文本在页面文本里定位
# ══════════════════════════════════════════════════════════════════════════

# 行首编号：`一、` `（二）` `7.1` `1.` —— 标题行几乎都带编号，正文段落不会
_NUMBER_PREFIX = re.compile(r"^(?:[（(][一二三四五六七八九十百零〇\d]{1,3}[）)]"
                            r"|[一二三四五六七八九十百零〇]{1,3}[、.．]"
                            r"|\d+(?:\.\d+)*[、.．]?)")
_CHAPTER_PREFIX = re.compile(r"^第[一二三四五六七八九十百零〇\d]{1,3}[章]")


def _match_key(text: Any) -> str:
    """比对小节用的骨架：去编号、去括号说明、去标点。

    章节标题与 `format_sections()` 给的 `title` 常常一个带编号一个不带
    （大纲里是 `六、投标报价一览表`，格式章里是 `投标报价一览表`），
    直接 `titles.index(title)` 会落空 → 找不到"下一节"，页码窗口一路开到章末，
    整章会把后面所有节的表都算进来。所以按骨架认人，不按字面认人。
    """
    clean = _NUMBER_PREFIX.sub("", _squash(text))
    clean = re.sub(r"[（(][^）)]{0,30}[）)]", "", clean)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", clean)


def _page_lines(page_text: str) -> list[str]:
    return [line for line in str(page_text or "").splitlines() if line.strip()]


def _bare(line: str) -> str:
    """去掉行首编号后剩下的标题本体（`一、投标函` → `投标函`）。"""
    return _NUMBER_PREFIX.sub("", _squash(line)).strip()


def _looks_like_heading(line: str, want: str) -> bool:
    """这一行是不是这一节的标题行：去编号后**就是**标题或以标题开头。

    必须按"行"比，不能按"页里出现过"比：`投标函` 三个字在「投标人须知」正文里
    反复出现，按整页包含会把节定位到 17 页（实测 14 节全部错位到须知章）。

    两边都去行首编号：`format_sections()` 给的标题本身带编号（`六、投标报价一览表`），
    而 `_bare()` 只会去掉**行**的编号 —— 只去一边就永远比不中（实测：节起点能命中，
    但"下一节在哪页"永远查不到，窗口一路开到章末 83 页）。
    """
    target = _bare(want)
    if not target:
        return False
    body = _bare(line)
    if not body:
        return False
    if body == target:
        return True
    # 标题后面可能跟括号说明（`分项报价表（格式）`），但不能再长——否则
    # `投标函` 会匹配到正文里以它开头的长句子
    return body.startswith(target) and len(body) <= len(target) + 8


def find_chapter_page(anchor: str, page_texts: Mapping[int, str]) -> int | None:
    """章标题所在页（`第六章 投标文件格式`）；只认"整行就是章标题"的那种。

    目录页里也有同一行，但后面跟着点线和页码（`第六章 投标文件格式......49`），
    去空白后不等于章标题本身，所以不会误判到目录页。
    """
    wanted = _squash(anchor)
    if not wanted:
        return None
    fallback = None
    for number in sorted(page_texts):
        for line in _page_lines(page_texts[number]):
            body = _squash(line)
            if body == wanted:
                return number
            if fallback is None and body.startswith(wanted) and len(body) <= len(wanted) + 4:
                fallback = number
    return fallback


def _window(titles: Sequence[str], page_texts: Mapping[int, str],
            chapter_anchor: str | None) -> tuple[int, int]:
    """可搜索的页码区间：有章锚点就收在章内，否则整篇。"""
    pages = sorted(page_texts)
    if not pages:
        return 1, 0
    if not chapter_anchor:
        return pages[0], pages[-1]
    first = find_chapter_page(chapter_anchor, page_texts)
    if first is None:
        return pages[0], pages[-1]
    # 章末 = 本章之后第一个"整行是第 N 章"的页，找不到就到最后
    last = pages[-1]
    for number in pages:
        if number <= first:
            continue
        for line in _page_lines(page_texts[number]):
            body = _squash(line)
            if _CHAPTER_PREFIX.match(body) and len(body) <= 20:
                last = number - 1
                break
        else:
            continue
        break
    return first, last


def _position_of(title: str, titles: Sequence[str]) -> int:
    """这一节的标题在 `titles` 里排第几：先按字面，再按骨架（带不带编号都认）。"""
    for index, item in enumerate(titles):
        if str(item or "") == str(title or ""):
            return index
    key = _match_key(title)
    if not key:
        return -1
    for index, item in enumerate(titles):
        if _match_key(item) == key:
            return index
    return -1


def locate_section_pages(title: str, *, titles: Sequence[str] = (),
                         page_texts: Mapping[int, str],
                         labels: Sequence[str] = (),
                         chapter_anchor: str | None = None) -> tuple[int, int] | None:
    """按标题文本定位这一节的页码区间 `(起, 止)`；定位不到返回 None。

    为什么不用固定页码：格式章页数因项目而异，写死页码换个标书就错位。
    为什么不用"整页包含标题"：实测 `投标函` 在投标人须知里反复出现，14 节会全部
    定位到 17 页附近。所以：**按行比对、去编号、限定在格式章范围内**。

    参数
    - `labels`：与 `titles` 对齐的带编号标题（`format_sections()` 给的 `label`，
      如 `一、投标函`）。有它时优先用它比对，命中率最高；没有就退回 `title`。
    - `chapter_anchor`：章标题文本（如 `投标文件格式`），用来把搜索收在格式章内。
    """
    if not page_texts:
        return None
    first, last = _window(titles, page_texts, chapter_anchor)
    pages = [number for number in sorted(page_texts) if first <= number <= last]
    if not pages:
        return None

    position = _position_of(title, titles)
    label = labels[position] if 0 <= position < len(labels) else ""
    wanted = _squash(title)

    def find(needle: str) -> int | None:
        """这一节标题**最后一次**作为整行出现的页。

        取"最后一次"而不是"第一次"：格式章开头一定有一页自己的目录（实测 p51
        「目 录」下把 13 个小节标题逐行列了一遍），第一次命中会全部落到目录页，
        14 节挤在同一页、表格全错。目录在前、正文标题在后，取最后即正文。
        """
        target = _squash(needle)
        if not target:
            return None
        hit = None
        for number in pages:
            for line in _page_lines(page_texts[number]):
                if _looks_like_heading(line, target):
                    hit = number
                    break
        return hit

    start = find(label) if label else None
    if start is None:
        start = find(title)
    if start is None:
        # 最后兜底：整页包含（会在须知章误命中，所以只在章窗口内、且没有行级命中时用）
        for number in pages:
            if wanted and wanted in _squash(page_texts[number]):
                start = number
    if start is None:
        return None

    # 终点 = 下一节标题所在页的前一页
    end = last
    later = list(titles[position + 1:]) if position >= 0 else []
    for offset, other in enumerate(later, start=position + 1):
        other_label = labels[offset] if offset < len(labels) else ""
        found = (find(other_label) if other_label else None) or find(other)
        if found is None:
            continue
        end = max(start, found - 1 if found > start else start)
        break
    return start, end


def tables_for_section(title: str, *, titles: Sequence[str],
                       page_texts: Mapping[int, str],
                       tables: Sequence[Mapping[str, Any]],
                       labels: Sequence[str] = (),
                       chapter_anchor: str | None = None) -> list[dict[str, Any]] | None:
    """这一节页码区间内检测到的表；标题定位不到返回 None（调用方降级）。"""
    span = locate_section_pages(title, titles=titles, page_texts=page_texts,
                                labels=labels, chapter_anchor=chapter_anchor)
    if span is None:
        return None
    start, end = span
    return [dict(table) for table in tables if start <= int(table.get("page") or 0) <= end]


def parse_pdf_tables(pdf_bytes: bytes, *, max_pages: int = 200) -> dict[str, Any]:
    """PDF 字节 → 解析报告（含逐页文本）。**一次解析、多次定位**用。

    生成链路一次要出十几个格式小节，如果每节都重新打开 PDF 解析一遍，
    83 页的标书会被解析十几遍（每遍 1~2 秒）。所以拆成"解析一次 + 逐节定位"。
    失败不抛异常，返回 `{"ok": False, "reason": ...}`。
    """
    from zhibiao.core.pdf_tables import extract_tables_from_pdf

    if not pdf_bytes:
        return {"ok": False, "reason": "no_pdf", "tables": [], "page_texts": {}}
    try:
        return extract_tables_from_pdf(pdf_bytes, max_pages=max_pages, with_page_texts=True)
    except Exception:  # noqa: BLE001 - 解析层已内部兜底，这里再兜一层
        return {"ok": False, "reason": "parse_error", "tables": [], "page_texts": {}}


def structure_from_report(report: Mapping[str, Any] | None, title: str, *,
                          titles: Sequence[str] = (), labels: Sequence[str] = (),
                          chapter_anchor: str | None = None) -> dict[str, Any]:
    """已解析的报告 + 节标题 → 这一节的结构化表；不行就返回带原因的失败字典。

    返回 `{"ok": bool, "reason": str, "tables": [...], "pages": [起, 止]}`，**不抛异常**。
    """
    fail = {"ok": False, "reason": "", "tables": [], "pages": []}
    if not report:
        return {**fail, "reason": "no_pdf"}
    if not report.get("ok"):
        return {**fail, "reason": str(report.get("reason") or "parse_error")}
    report_tables = list(report.get("tables") or [])
    if not report_tables:
        return {**fail, "reason": "no_tables_detected"}
    known = list(titles) or [title]
    page_texts = report.get("page_texts") or {}
    span = locate_section_pages(title, titles=known, page_texts=page_texts,
                                labels=labels, chapter_anchor=chapter_anchor)
    if span is None:
        return {**fail, "reason": "section_not_located"}
    picked = tables_for_section(title, titles=known, page_texts=page_texts,
                                tables=report_tables, labels=labels,
                                chapter_anchor=chapter_anchor) or []
    if not picked:
        return {**fail, "reason": "no_tables_for_section", "pages": [span[0], span[1]]}
    return {"ok": True, "reason": "", "tables": picked, "pages": [span[0], span[1]]}


def structure_for_section(pdf_bytes: bytes, title: str, *, titles: Sequence[str] = (),
                          labels: Sequence[str] = (), chapter_anchor: str | None = None,
                          max_pages: int = 200) -> dict[str, Any]:
    """PDF 字节 + 节标题 → 这一节的结构化表（单节场景的便捷入口）。

    一次只出一节的调用方（测试、脚本）用这个；生成链路用
    `parse_pdf_tables()` + `structure_from_report()`，避免重复解析整份 PDF。
    """
    return structure_from_report(parse_pdf_tables(pdf_bytes, max_pages=max_pages), title,
                                 titles=titles, labels=labels, chapter_anchor=chapter_anchor)