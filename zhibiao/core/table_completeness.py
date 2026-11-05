"""「技术参数/技术要求逐条响应」对照表的完整性处置：半截表要么删掉，要么补齐。

线上事故（v6/v7 实测的两种形态）：
- 第 10 章导言末尾那张《技术参数逐条响应对照表》只写到一半就结束 —— v6 是序号 1、2 两行，
  v7 只剩序号 1 一行（驱动方式）；而 10.4.3 里同样一张表完整写了 1～15 条。评委按导言那张
  表核对，会认定投标人只响应了 1～2 条；更糟的是成稿里同时存在两张自相矛盾的表。
- 第 10.7.5《供货范围责任分工与节点》只有 `| 阶段 | 工作内容 |` 一行表头，下面一行数据都
  没有：既没内容又吓人（评委看到的是「投标人交了一张空表」）。

判据与处置（全部是机械判定，不调模型、不臆断内容）：
1. 只认**表头含「对照/响应/应答」**的表格 —— 那是「逐条响应」类表格的表征；
2. 从招标原文抽出对应的条目清单（技术参数/技术要求表，或编号条目），得到应有条目数 N；
   N 小于 `MIN_EXPECTED` 说明这份招标文件根本没给出逐条清单，判不了，直接放过；
3. **不完整表**的判据：`1 <= 行数 < N`，**且表的前几行就是招标原文条目的前几条**
   （「逐条写到一半断了」的签名，见 `_looks_like_source_prefix`）。
   线上 v6 实测：15 条原文只写了序号 1～8 的对照表（8/15 ≈ 53%），旧判据「行数 < N/2」
   判不出来，故不再用比例下限，改为「比应有的少、且是原文前缀」即算不完整：
   - 同章（任一位置）或后文章节已有同类**完整表**（行数 ≥ `COMPLETE_RATIO`·N ≈ 0.9N）
     → 删掉不完整表，连同它的表头、分隔行和空行；保留那张完整表；
   - 没有完整表 → 按招标原文条目名补齐缺失条目（序号沿用原文编号），
     响应值/偏离值留占位（不编造）；
4. **单行残表**（v7 缺陷：旧判据要求「行数 ≥ 2」，只剩 1 行的表整张漏网）：
   - 行数 == 1 **且**它是本章唯一/最靠前的那张对照表 **且** 同章/后文已有同类完整表
     → 这张表连一句完整条目都凑不出，留着只会自相矛盾：删表（连表头/分隔行/空行），
     并把它上方那句只为它写的引导语（含「下表」「逐条对应」「第 1 至 15 条」等）一并删掉，
     免得正文里留一句指向空处的「见下表」；
   - 反过来，全文只有这一张 1 行表、后文没有完整表 → **不删**，按原文条目补齐到 N 行
     （沿用第 3 条的 pad 逻辑；单行还必须是原文第 1 条，否则证据不足，交人工）；
5. **空表体**（表头 + 分隔行 + 0 行数据）单独处置，不要求表头含「对照/响应/应答」：
   - 能从招标原文取到**本节**条目（对照/响应表取全册技术参数清单 N；其它表按本节标题
     去招标原文找同名的编号清单）→ 按条目补齐行，值列填 `{{待填:…}}`；
   - 取不到本节条目 → 把这行空表换成一句中性说明（不留只有表头的空表）；
   - 表头 + 若干 `{{待填:…}}` 行是正常的「待填表」，不是空表体，不动；
6. 防误删（宁可不改，也不误改正确内容）：
   - 行数 ≥ N 的表不动（已完整）；
   - 表头不含「对照/响应/应答」的表不动（不是逐条响应表）；
   - 规模明显小于原文的「概要表/摘自表」（行数 < N/3 **且**不是从原文第 1 条开始的
     连续前缀）不动 —— 它们本来就不是「逐条列全」的东西；
   - 条目与原文对不上的表不动（`_looks_like_source_prefix` 不成立）；
7. 幂等：补齐后的表行数已达 N（或已非空），第二次执行不再命中；删除/换成说明后表已不
   存在，同样不再命中。
8. **删除/补行必须是「最小且可证明」的**（`relocate_table`）：`Table.start/end` 是行号，
   行号只在算出它的那份文本里有效。同一章里先后处置两张表时，前一次删除会让后面每一行
   的行号前移；旧实现每次删除后还对整篇跑 `\n{3,}` 归一，正文别处的两连空行也会让行号漂。
   于是第二次按旧行号切片就删到表格之外的正文（实测把表后的 `10.4.1` 小节标题删掉、却把
   残表的表头留在正文里）。现在每次动手前都在**当前文本**里重新定位表格，断言区间起止都
   落在表格行内（每一行都以 `|` 开头）、表头与表体逐格相同，才允许删/补；定不到就不动、
   只报（`reconcile_response_tables` 返回项的 `detail` 里写明「未改动，交人工」）。
   切口处只收空行，区间外一个字不动。
9. **表不得跨节合并**（v9 事故 A）：`iter_tables` 的「连续 `|` 行算一张表」只在一种情况下
   成立 —— 两张表之间**什么都没有**。真实成稿里相邻两张表常常连空行都没有，旧实现就把
   后一张的表头当成前一张的一行数据、把它后面的行算成前一张的行（v9：1.1 的《采购对象表》
   后面紧跟第五章第四节那张 20 条标准的表，被当成同一张 23 行表）。现在块内出现「整行
   都是短列名，且下一行序号从 1 重新起算」而此前数据行序号已到 ≥2 时，就地切开
   （`_split_block`）—— 一张表只能由它自己的表头与表体组成。
10. **补行不许跨节**（v9 事故 A 的真正入口）：补行用的条目清单必须与这张表**同一主题**
   （原文候选清单的上下文标题与表头至少有 2 个二字词重合，见 `_subject_items`）。旧实现
   直接取全书最长的一组条目：1.1《采购对象表》表头有一列「技术要求」、那一列的值恰好是
   标准号，`_name_column` 就选中了它，而 `tender_requirement_items` 取的是全书最长的
   第五章第四节 20 条标准 → 判定「3 行都是原文前缀」→ 把 20 条标准按序号 4~23 灌进采购
   对象表（其余列写成 `{{待填:表头}}`）。跨节补行一律不补，只报。
11. **序号只保留、不重排**（v9 事故 B）：补出来的行号必须是**招标原文条目自己的序号**
   （旧实现是「表内行数 + 1」：表里本来跳号时会与已有行重号，还会把已有那一行再写一份
   `{{待填}}` 版盖住原文数值）。因此补行前先验「表内序号是从 1 开始的连续编号」
   （`_sequence_gap`）：跳号/重号/行首不是序号的一律不补、不重排、不删，交人工 ——
   自动重排会让丢行彻底看不出来。
12. **原文数值不许被占位覆盖**：要补的条目如果已经在表内（同一格文字已出现过），不补
   （见 `_pad_blockers`）；补行只往表尾追加、绝不改写已有行；条目名照原文逐字搬运，
   不截断（旧实现 `name[:60]` 把两条长标准名抄成了半截）。

阈值都写成模块常量，便于按实测调整；函数是纯函数（入参出参都是字符串/字典），
不做 IO、不写库，落库由调用方 `generation_runner` 负责。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# 完整表：行数 ≥ 招标条目数 × 该比例（任务口径 0.9N）
COMPLETE_RATIO = 0.9
# 招标原文条目数少于该值时判不了逐条清单是否列全，直接放过（避免 2 条清单把 1 行表判成不完整）
MIN_EXPECTED = 4
# 「多行半截表」的行数下限：≥2 行且是原文连续前缀才机械处置（判据见 `_is_incomplete`）
MIN_ROWS = 2
# 单行残表：行数 == 1 的对照表也纳入处置（v7 实测导言表只剩「驱动方式」一行）。
# 单行表只在两种情况下动：本章唯一/最靠前的对照表且后文已有完整表（删），或它正是原文
# 第 1 条且全文没有完整表（补）；其余单行表证据不足，交人工（`_is_excerpt` 会放过）。
RESIDUE_ROWS = 1
# 「概要表/摘自表」的规模上限：行数 < N / EXCERPT_DIVISOR，且不是从第 1 条开始的前缀 → 不动
EXCERPT_DIVISOR = 3
# 补齐条目时响应列的占位写法（与全书 {{待填:字段}} 口径一致，导出时转红色待填）
PLACEHOLDER = "{{待填:投标响应}}"
# 空表体换成的中性说明：分工类表说「分工」，其它表说「内容」，都不指向空表、也不臆断内容；
# 一律**不承诺**（旧文案写死「在合同签订阶段确认」，被插进本该填数据的小节里很刺眼）
EMPTY_TABLE_NOTE = "本节按采购文件格式随附，具体分工待确认后填写。"
EMPTY_TABLE_NOTE_GENERIC = "本节按采购文件格式随附，具体内容待确认后填写。"

# 表头里出现这些词，才算「逐条对照/响应」表
_RESPONSE_MARKERS = ("对照", "响应", "应答")

_SEPARATOR = re.compile(r"^\s*\|[\s:|-]*-{2,}[\s:|-]*$")

_INDEX_COL = re.compile(r"序\s*号|编号|^no\.?$", re.I)
_NAME_COL = re.compile(r"名称|项目|内容|条目|技术参数|技术要求|规格|指标|参数|要求|条款")
_RESPONSE_COL = re.compile(r"响应|应答|对照|投标响应")
_DEVIATION_COL = re.compile(r"偏离|偏差|差异")

# 引出表格的那句话（「见下表」「逐条对应」「第 1 至 15 条」…）：删残表时连它一起删，
# 免得正文里剩一句指向空处的「如下表所示」
_LEAD_IN = re.compile(
    r"下表|下表中|如下表|上表|如图|如下|逐条对应|逐条对照|逐条响应|对照表|响应表|应答表"
    r"|详见|第\s*\d{1,3}\s*[至到~～—\-－]\s*\d{1,3}\s*条")
# 小节标题：编号标题（10.4.3 / 一、/ 第一章 / Markdown #）都不是「引出句」，不许当引导语删掉
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s|\d+(?:\.\d+)*\s*[、.．]?\s*[^\d\s]|第[一二三四五六七八九十百\d]{1,4}[章节部分篇]"
    r"|[一二三四五六七八九十]{1,4}\s*[、.．])")
# 分工/节点类表头：空表体换成说明时用「分工」那句，其余用「内容」那句
_SPLIT_HINT = re.compile(r"分工|职责|责任|节点|阶段")

# 招标原文里「技术参数/技术要求」类小节标题：编号条目的计数只在这些小节里做
_TECH_HINT = re.compile(r"技术参数|技术要求|技术规格|技术指标|技术性能|参数要求"
                        r"|规格参数|参数表|技术清单|技术需求|技术要求表")
_NUMBERED = re.compile(
    r"^\s*(?:(?P<num>\d{1,3})\s*[、.．)）]|[（(]\s*(?P<paren>\d{1,3})\s*[）)])\s*(?P<name>\S.{0,58}?)\s*$")
_SECTION = re.compile(r"^\s*(?:第[一二三四五六七八九十百\d]{1,4}[章节部分篇]|附件\s*[\d一二三四五六七八九十]"
                      r"|[一二三四五六七八九十]{1,3}\s*[、.．])")

# ── 切表/补行用的结构常量（全部是形状判据，不含任何具体文档的词）──────────────
# 列名必须短、且不带句读；「型号」「标准号」都是列名，「100 万元」「1 组」不是
_MAX_LABEL_CHARS = 14
_CELL_PUNCT = "。；，、！？：:;,.!?"
# 行首序号：`1` / `1.` / `1、` / `1)` / `1）`（补行与「序号跳号」判据都用它）
_ROW_NUMBER = re.compile(r"^(\d{1,3})\s*[.．、)）]?$")
# 表内序号与原文条目名比的比较基准：去掉全部空白（PDF 断行/空格不该影响判等）
_SPACES = re.compile(r"[\s\u3000]+")
# 非数据行（合计/小计/备注/说明/注）：补行不许落在它们之后
_NON_DATA_FIRST = ("合计", "小计", "总计", "共计", "合价", "总价", "备注", "说明", "注")


# ══════════════════════ 一、Markdown 管道表格解析 ══════════════════════

@dataclass(frozen=True)
class Table:
    """正文里的一张管道表格（行号是 `str.splitlines()` 的下标，end 不含）。"""

    start: int
    end: int
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    separators: int = 0

    @property
    def row_count(self) -> int:
        return sum(1 for row in self.rows if any(cell.strip() for cell in row))

    @property
    def kind(self) -> tuple[str, ...]:
        """同类表的判定键：命中的表头标记 + 列数（列数容差 ±1 由 `_same_kind` 处理）。"""
        markers = tuple(sorted({word for word in _RESPONSE_MARKERS
                                if any(word in cell for cell in self.header)}))
        return markers + (str(len(self.header)),)


def _is_pipe_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return tuple(cell.strip() for cell in stripped.split("|"))


def iter_tables(text: str) -> list[Table]:
    """解析正文里所有管道表格（连续的「|」行算一张表，分隔行不算行）。

    「连续」不等于「同一张」：v9 事故 A 里 1.1 的《采购对象表》后面**没有空行**，
    紧跟第五章第四节那张 20 条标准的表；旧实现把两张并成一张（第二张的表头变成第一张
    的一行数据），后面所有「补行/删行」就都在按错的表做。这里先用 `_split_block` 把
    「块里还有一张表」的块切开：切点只用结构证据 —— 一整行都是短列名（表头形状）、
    且紧接的下一行序号从 1 重新起算，而此前数据行的序号已经走到 ≥2。
    两张表之间有正文行/标题/空行的，本来就是两张表，行为与历史完全一致。
    """
    lines = str(text or "").splitlines()
    tables: list[Table] = []
    index = 0
    while index < len(lines):
        if not _is_pipe_row(lines[index]):
            index += 1
            continue
        start = index
        while index < len(lines) and _is_pipe_row(lines[index]):
            index += 1
        for begin, end in _split_block(lines, list(range(start, index))):
            header: tuple[str, ...] = ()
            rows: list[tuple[str, ...]] = []
            separators = 0
            for position in range(begin, end):
                if _SEPARATOR.match(lines[position]):
                    separators += 1
                    continue
                cells = _cells(lines[position])
                if not header:
                    header = cells
                else:
                    rows.append(cells)
            if header:
                tables.append(Table(start=begin, end=end, header=header, rows=tuple(rows),
                                    separators=separators))
    return tables


def _row_number(cells: Sequence[str]) -> int | None:
    """行首序号：第一格是 `1` / `1.` / `1、` 这类纯编号时返回它，否则 None。"""
    if not cells:
        return None
    found = _ROW_NUMBER.match(str(cells[0] or "").strip())
    return int(found.group(1)) if found else None


def _is_column_like(text: Any) -> bool:
    """这一格像不像列名：短、不带句读、不是纯编号、不是 `{{待填}}` 占位。"""
    clean = str(text or "").strip()
    if not clean or len(clean) > _MAX_LABEL_CHARS:
        return False
    if "{{" in clean or "}}" in clean:
        return False
    if _ROW_NUMBER.match(clean):
        return False
    return not any(ch in clean for ch in _CELL_PUNCT)


def _is_second_header(cells: Sequence[str]) -> bool:
    """这一行是不是**另一张表**的表头：整行都是短列名（不含纯编号、不含占位）。"""
    filled = [cell for cell in cells if str(cell or "").strip()]
    return len(filled) >= 2 and all(_is_column_like(cell) for cell in filled)


def _split_block(lines: Sequence[str], positions: Sequence[int]) -> list[tuple[int, int]]:
    """一块连续「|」行 → 若干张表的行区间 `[start, end)`（只用结构证据切）。

    切点判据（三条同时成立，见模块说明第 9 条）：
      1. 这一行整行都是短列名（`_is_second_header`）—— 它是表头，不是数据行；
      2. 紧接的下一行序号从 1 重新起算；
      3. 此前已经出现过序号 ≥2 的数据行 —— 说明编号是被「重新起算」而不是原本从 1 起。
    只切一条边界的证据不足（页脚重复表头后接着 11、12 的续页不算新表），所以不含第 2 条
    的一律不切：宁可少切，也不把一张表拆成两张。
    """
    if not positions:
        return []
    rows = [(position, _cells(lines[position])) for position in positions
            if not _SEPARATOR.match(lines[position])]
    cuts: list[int] = []
    if len(rows) >= 3:
        reached = 0                      # 本张表里出现过的最大序号
        for order in range(1, len(rows) - 1):
            position, cells = rows[order]
            number = _row_number(cells)
            if number is not None:
                reached = max(reached, number)
                continue
            if not _is_second_header(cells):
                continue
            if _row_number(rows[order + 1][1]) != 1 or reached < 2:
                continue
            cuts.append(position)
            reached = 0
    bounds = [positions[0]] + cuts + [positions[-1] + 1]
    return [(bounds[index], bounds[index + 1]) for index in range(len(bounds) - 1)
            if bounds[index] < bounds[index + 1]]


def is_response_table(header: Sequence[str]) -> bool:
    """表头是否属于「技术参数/技术要求逐条对照（响应）」类表格。"""
    return any(word in str(cell or "") for cell in header for word in _RESPONSE_MARKERS)


def _same_kind(left: Table, right: Table) -> bool:
    """同类表：表头标记一致，且列数相差不超过 1 列（模型偶尔多/少一列「备注」）。"""
    left_markers, right_markers = left.kind[:-1], right.kind[:-1]
    if not left_markers or left_markers != right_markers:
        return False
    return abs(len(left.header) - len(right.header)) <= 1


# ══════════════════════ 二、招标原文条目清单 ══════════════════════

def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text or ""))
    return {clean[index:index + 2] for index in range(len(clean) - 1)}


def _overlap(left: str, right: str) -> int:
    if not left or not right:
        return 0
    return len(_bigrams(left) & _bigrams(right))


def _name_column(header: Sequence[str]) -> int:
    """条目名所在列：优先「名称/项目/技术参数/要求…」，否则取序号列之后的第一列。"""
    for index, cell in enumerate(header):
        if _INDEX_COL.search(str(cell or "")):
            continue
        if _NAME_COL.search(str(cell or "")):
            return index
    for index, cell in enumerate(header):
        if not _INDEX_COL.search(str(cell or "")):
            return index
    return -1


def is_item_list_table(header: Sequence[str]) -> bool:
    """表头是否长得像「逐条清单表」：有序号列 + 条目列（招标原文的技术参数表就是这个形状）。"""
    header_text = " ".join(str(cell or "") for cell in header)
    return bool(_INDEX_COL.search(header_text)) and bool(_NAME_COL.search(header_text))


def _table_items(table: Table) -> list[str] | None:
    """一张招标表格 → 条目名清单（不像技术参数/要求表就返回 None）。"""
    if not is_item_list_table(table.header):
        return None
    column = _name_column(table.header)
    if column < 0:
        return None
    names: list[str] = []
    for row in table.rows:
        if column >= len(row):
            continue
        value = str(row[column] or "").strip(" 　:：")
        if not value or value.isdigit():
            continue
        names.append(value)
    return names or None


def _numbered_items(lines: list[str], start: int) -> list[str]:
    """从 `start` 行的下一行起，按连续编号收集条目名（编号回退或串号即停）。

    只收在「技术参数/技术要求」类小节标题之后、且编号连号的条目：
    招标文件里这样的清单才是「逐条响应」的对照对象。
    """
    collected: list[str] = []
    expect = 1
    for follow in lines[start + 1:start + 121]:
        found = _NUMBERED.match(follow)
        if not found:
            if collected and follow.strip() and _SECTION.match(follow):
                break
            continue
        try:
            number = int(found.group("num") or found.group("paren"))
        except (TypeError, ValueError):
            continue
        name = found.group("name").strip(" 　:：、")
        if number == 1 and collected:
            break
        if number != expect:
            if number < expect:
                continue
            break
        collected.append(name)
        expect += 1
    return collected


def _heading_like(line: str) -> bool:
    """一行是不是小节标题（编号标题 / 第 X 章 / 一、/ Markdown # / 含「技术参数」字样）。"""
    text = str(line or "").strip()
    if not text or len(text) > 60 or "|" in text:
        return False
    return bool(_SECTION.match(text) or _HEADING.match(text) or _TECH_HINT.search(text))


def _requirement_candidates(tender_text: str, *,
                            any_section: bool = False) -> list[tuple[str, list[str]]]:
    """招标原文里所有「像逐条清单」的候选：`(上下文标题, 条目名清单)`。

    `any_section=False`（默认）只在「技术参数/技术要求」类小节里认编号条目 —— 那才是
    「逐条响应」的对照对象；`any_section=True` 放宽到任意小节标题，用于给空表体找「本节条目」。
    """
    text = str(tender_text or "")
    if not text.strip():
        return []
    candidates: list[tuple[str, list[str]]] = []
    for table in iter_tables(text):
        items = _table_items(table)
        if items:
            candidates.append((" ".join(table.header), items))
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 60 or "|" in stripped:
            continue
        if not _TECH_HINT.search(stripped) and not (any_section and _heading_like(stripped)):
            continue
        items = _numbered_items(lines, index)
        if items:
            candidates.append((stripped, items))
    return candidates


def tender_requirement_items(tender_text: str, *, hint: str = "") -> list[str]:
    """招标原文里「技术参数/技术要求」的逐条清单（去重后最长的一组）。

    `hint`（一般是章节标题）用于在多组候选里优先挑语义相近的那组；
    多组条目数相同时才按 hint 决定，不凭 hint 编造条目。
    """
    candidates = _requirement_candidates(tender_text)
    if not candidates:
        return []
    preferred = [item for item in candidates if hint and _overlap(item[0], hint) >= 2]
    _context, names = max(preferred or candidates, key=lambda item: len(item[1]))
    return list(dict.fromkeys(names))


def section_requirement_items(tender_text: str, *, hint: str = "") -> list[str]:
    """招标原文里**与本小节标题对得上**的条目清单；对不上就返回空（不拿别节的清单来凑）。

    空表体要「按本节条目补齐」时用这个：只有标题（如「10.7.5 供货范围责任分工与节点」）
    与招标原文某个编号清单的小节标题语义相近，才认为「该节有条目」；否则宁可换一句中性说明，
    也不把技术参数清单硬塞进供货范围表。
    """
    heading = str(hint or "").strip()
    if not heading:
        return []
    matched = [items for context, items in _requirement_candidates(tender_text, any_section=True)
               if _overlap(context, heading) >= 2]
    if not matched:
        return []
    return list(dict.fromkeys(max(matched, key=len)))


def _compact(text: Any) -> str:
    """比较用：去掉全部空白（PDF 断行/空格不该影响「是不是同一格文字」）。"""
    return _SPACES.sub("", str(text or ""))


def _subject_items(candidates: Sequence[tuple[str, list[str]]], table: Table, *,
                   hint: str = "") -> list[str]:
    """与这张表**同一主题**的原文条目清单；对不上就返回空（宁可不补，也不跨节补行）。

    为什么必须对主题（v9 事故 A）：旧实现直接拿全书「最长的那组条目」来补行。1.1
    《采购对象表》表头里有一列「技术要求」，那一列的值恰好是标准号 —— 于是
    `_name_column` 选中了「技术要求」列、`tender_requirement_items` 又拿的是全书最长的
    第五章第四节那 20 条标准，`_looks_like_source_prefix` 判「3 行都是原文前缀」成立，
    20 条标准就被按序号 4~23 灌进了采购对象表（其余列全是 `{{待填:表头}}`）。
    补行只允许补「同一主题」的清单：候选清单的上下文标题（表头行或小节标题）与这张表的
    表头至少重合 2 个二字词，才算同一主题；章标题（`hint`）只在表头对不上时兜底。
    """
    header = " ".join(str(cell or "") for cell in table.header)
    matched = [items for context, items in candidates if _overlap(context, header) >= 2]
    if not matched and hint:
        matched = [items for context, items in candidates if _overlap(context, hint) >= 2]
    if not matched:
        return []
    return list(dict.fromkeys(max(matched, key=len)))


def _sequence_gap(table: Table) -> str:
    """表内序号是不是从 1 开始的连续编号；不是就返回原因（原因非空 = 不许补行）。

    为什么（v9 事故 B）：补行是「接着表内行数写序号」。表里本来就跳号（实测端尾备件表
    只剩序号 2、4、6、7、8）时，补出来的行会与已有行重号，还会把已有那一行再写一份
    `{{待填}}` 版盖住原文给的数值 —— 既丢信息又掩盖丢行。跳号一律**不补、不重排**，
    交人工：自动重排会让「少了几行」彻底看不出来。
    「合计/小计/备注/说明」这类行不是数据行：它们在表里时，补出来的行会落在合计之后
    （加总关系就错了），同样不补。
    """
    if table.row_count == 0:
        return ""
    numbers: list[int] = []
    for row in table.rows:
        if not any(str(cell or "").strip() for cell in row):
            continue
        first = str(row[0] or "").strip() if row else ""
        if first.startswith(_NON_DATA_FIRST):
            return "表内含「%s」这类非数据行，补行会落在它之后（加总关系会错）" % first[:8]
        number = _row_number(row)
        if number is None:
            return "表内行首不是序号，无法证明补上去的行接在原有行之后"
        numbers.append(number)
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        return ("表内序号跳号/重号（%s），保留原序号、不自动重排"
                % "、".join(str(number) for number in numbers[:8]))
    return ""


def _pad_blockers(candidates: Sequence[tuple[str, list[str]]], table: Table, *,
                  hint: str = "") -> tuple[list[str], str]:
    """补行前的三道硬闸：返回 (同一主题的原文条目, 不许补的理由)。

    理由非空表示这一张表一个字都不许补（但删除那张残表的判据不受影响，见
    `reconcile_response_tables`）；理由只进 `detail`，不写进正文。
    """
    items = _subject_items(candidates, table, hint=hint)
    if not items:
        return [], "表头与招标原文的条目清单对不上，禁止跨节补行"
    gap = _sequence_gap(table)
    if gap:
        return items, gap
    listed = {_compact(cell) for row in table.rows for cell in row}
    for name in items[table.row_count:]:
        key = _compact(name)
        if key and key in listed:
            return items, ("要补的条目「%s」已在表内（补行会重复，并用 {{待填}} 盖住原文数值）"
                           % str(name)[:24])
    return items, ""


# ══════════════════════ 三、处置：删半截表 / 补齐条目 ══════════════════════

def _complete_rows(expected: int) -> int:
    return max(2, int(expected * COMPLETE_RATIO + 0.999))


def _is_excerpt(table: Table, items: Sequence[str]) -> bool:
    """规模明显小于原文的「概要表/摘自表」：行数 < N/3 且不是从第 1 条开始的连续前缀。

    这类表本来就不打算「逐条列全」（例如只摘 3 条重点、或从第 5 条起摘录一段），
    不属于「写到一半断了」，一律不动 —— 动的风险远大于收益。
    """
    if not items:
        return False
    if table.row_count * EXCERPT_DIVISOR >= len(items):
        return False
    return not _starts_at_first_item(table, items)


def _starts_at_first_item(table: Table, items: Sequence[str]) -> bool:
    """表的第一条内容是否就是招标原文第 1 条（「从第 1 条开始逐条列」的签名）。"""
    if not items:
        return False
    column = _name_column(table.header)
    if column < 0:
        return False
    for row in table.rows:
        name = str(row[column]).strip() if column < len(row) else ""
        if name:
            return _overlap(name, items[0]) >= 2
    return False


def _is_incomplete(table: Table, items: Sequence[str]) -> bool:
    """不完整表：`1 <= 行数 < N` 且表内条目是招标原文前若干条的连续前缀。

    v6 缺陷在这条判据上：15 条原文只写了 1～8 行（8/15 ≈ 53%），
    旧判据「行数 < N/2」漏掉了它；现在只要比应有的少、且是原文前缀就算不完整。
    v7 缺陷在行数下限上：只剩 1 行的残表被「行数 ≥ 2」整张放过 —— 1 行表只要**正是原文
    第 1 条**（断在第 2 条之前）同样算不完整；对不上原文的单行表证据不足，仍交人工。
    行数 ≥ N（已完整）不算。
    """
    if not items or table.row_count < RESIDUE_ROWS or table.row_count >= len(items):
        return False
    if table.row_count < MIN_ROWS and not _starts_at_first_item(table, items):
        return False
    return _looks_like_source_prefix(table, items)


def _is_leading_residue(table: Table, position: int) -> bool:
    """只剩 1 行、且是本章唯一/最靠前的那张对照表（v7 缺陷签名）。

    导言末尾用一句「见下表」引出对照表，结果只写出了序号 1 一行就断了；这种残表本身
    凑不出一条完整清单，只要后文另有完整表，它唯一的作用就是让评委以为只响应了 1 条。
    只看「最靠前」的一张：后面的 1 行表更可能是摘自表/小结表，不属于本节导言的残表。
    """
    return table.row_count == RESIDUE_ROWS and position == 0


def _is_empty_body(table: Table) -> bool:
    """空表体：有表头、有分隔行、但一行数据都没有（v7 实测 `| 阶段 | 工作内容 |` 下面空白）。

    要求 ≥2 列且确实有 Markdown 分隔行：单独一行「|」不构成表格，不该被当成空表删掉。
    """
    return table.row_count == 0 and len(table.header) >= 2 and table.separators >= 1


def _looks_like_source_prefix(table: Table, items: Sequence[str]) -> bool:
    """这张表的条目前几行是否就是招标原文条目的前几行（「写到一半断了」的签名）。

    为什么要这一层：「导言里放一张 3 行的概要表」本身是正常写法，它不是半截表。
    真正的半截表有明确签名 —— 它的第 1 行对应原文第 1 条、第 2 行对应第 2 条……
    断在第 N 行。只有这种「逐条对照但没写完」的表才该删/补；
    用词与原文对不上的概要表一律不动（宁可留给人工，也不机械改写正确内容）。
    """
    column = _name_column(table.header)
    if column < 0:
        return False
    names = [str(row[column]).strip() for row in table.rows
             if column < len(row) and str(row[column]).strip()]
    if not names or len(names) > len(items):
        return False
    matched = sum(1 for index, name in enumerate(names)
                  if _overlap(name, items[index]) >= 2)
    return matched >= max(1, int(len(names) * 0.6 + 0.999))


def _has_complete_copy(responses: list[list[Table]], chapter: int, position: int,
                       table: Table, expected: int) -> bool:
    """同章（任一位置）或后文章节里是否已有同类完整表（行数 ≥ 0.9N）。

    有完整表时删掉不完整的那张即可，不必补 —— 补了反而会出现两张完整表自相矛盾。
    只看同章与后文：前面章节的完整表不能替本章作主（本章仍应逐条列全）。
    """
    need = _complete_rows(expected)
    for sibling_position, other in enumerate(responses[chapter]):
        if sibling_position != position and _same_kind(table, other) \
                and other.row_count >= need:
            return True
    for later in responses[chapter + 1:]:
        for other in later:
            if _same_kind(table, other) and other.row_count >= need:
                return True
    return False


def is_lead_in(line: str) -> bool:
    """这一行是不是「为了引出下面那张表」的引导语（含「下表」「逐条对应」「第 1 至 15 条」…）。

    只认句子，不认标题：`10.4.3 技术参数逐条响应表` 这种小节标题命中了「响应表」也不能删，
    否则会把小节标题删掉。过长的一行（>80 字）也不当引导语 —— 那多半是正文段落。
    """
    text = str(line or "").strip()
    if not text or len(text) > 80:
        return False
    if text.startswith("#") or _SEPARATOR.match(text) or _is_pipe_row(text):
        return False
    if _HEADING.match(text):
        return False
    return bool(_LEAD_IN.search(text))


def _lead_in_index(lines: Sequence[str], start: int) -> int | None:
    """表格上方最近那个非空行是引导语就返回它的下标（跳过空行），否则 None。"""
    index = start - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0 or not is_lead_in(lines[index]):
        return None
    return index


# ══════════════════════ 三·五、删除前重定位（越界防线） ══════════════════════

def _same_table(left: Table, right: Table) -> bool:
    """两张表是不是同一张（表头与表体逐格相同）。"""
    return left.header == right.header and left.rows == right.rows


def _span_is_table_only(lines: Sequence[str], table: Table) -> bool:
    """断言 `[start, end)` 只覆盖表格行：起止都落在表格行内，区间里没有一行不是「|」行。"""
    if not (0 <= table.start < table.end <= len(lines)):
        return False
    return all(_is_pipe_row(line) for line in lines[table.start:table.end])


def relocate_table(text: str, table: Table) -> Table | None:
    """在**当前文本**里重新定位这张表；定不到或区间不是纯表格行 → None（调用方不删，只报）。

    为什么必须重定位：`Table.start/end` 是行号，行号只在算出它的那份文本里有效。
    同一章里先后处置两张表时，前一次删除（以及切口处空行的归一）会让后面每一行的行号
    整体前移，再按旧行号切片就会删到表格之外的正文。实测形态：一章里有「残表一」「残表二」
    两张半截表 + 后文那张完整表时，处置完第二张后行号前移 3 行，第一张的删除区间落到
    「表体末行 / 一格正文 / `10.4.1 小节`标题 / `残表二见下表：`」上 —— 标题与引导句被删掉，
    而残表一的表头却留在正文里（既丢字又留脏表）。

    判据只有结构性证据：表头 + 表体逐格相同、且区间内每一行都是管道表格行。
    同一章里有两张一模一样的表时取行号最近的一张（内容相同，删哪张都是同一结果）。
    """
    lines = str(text or "").splitlines()
    candidates = [item for item in iter_tables(text) if _same_table(item, table)]
    if not candidates:
        return None
    fresh = min(candidates, key=lambda item: abs(item.start - table.start))
    return fresh if _span_is_table_only(lines, fresh) else None


def _splice(lines: Sequence[str], start: int, end: int,
            insert: Sequence[str] = ()) -> str:
    """把 `lines[start:end]` 换成 `insert`：只在切口处收空行，区间外一个字不动。

    旧实现每次删除后对**整篇**跑 `re.sub(r"\\n{3,}", "\\n\\n")`：正文别处只要有两连空行
    就会被折掉一行，于是同章后一次删除按旧行号切片时整体错位（见 `relocate_table`）。
    现在只在切口两侧各留一个空行，别处的空行原样保留。
    """
    head = list(lines[:start])
    while head and not head[-1].strip():
        head.pop()
    tail = list(lines[end:])
    while tail and not tail[0].strip():
        tail.pop(0)
    body = list(head)
    chunk = list(insert)
    if body and chunk:
        body.append("")
    body.extend(chunk)
    if tail:
        if body:
            body.append("")
        body.extend(tail)
    return "\n".join(body).strip("\n")


def remove_table_with_lead_in(text: str, table: Table) -> str:
    """删掉表格 + 它上方那句只为它写的引导语（一次成文，两张表的行号不会相互失效）。

    引导句是残表存在时写下的（「本章技术参数逐条响应对照表见下表」），表都删了还留着，
    正文里就多一句指向空处的「见下表」。上方的段落正文、下方的内容逐字保留。

    删除前在**当前文本**里重新定位并断言区间只覆盖表格行（`relocate_table`）；定不到就
    原样返回（不删，只报）。引导语与表头之间只允许是空行，否则不删引导语。
    """
    fresh = relocate_table(text, table)
    if fresh is None:
        return str(text or "")
    lines = str(text or "").splitlines()
    start, end = fresh.start, fresh.end
    while end < len(lines) and not lines[end].strip():
        end += 1
    lead = _lead_in_index(lines, start)
    if lead is not None and all(not lines[index].strip() for index in range(lead + 1, start)):
        start = lead
    return _splice(lines, start, end)


def replace_table_with_note(text: str, table: Table, note: str) -> str:
    """把空表体换成一句中性说明：能认出为它写的引导语就替换那一句，否则原位留说明。

    只删表、不删引导语会留下「如下表所示：」却无表；所以引导语优先被替换成说明。
    换出来的是一行普通正文，第二次执行不会再被当成表格，天然幂等。
    与删除同源：先在当前文本里重定位，定不到就原样返回（不删，只报）。
    """
    note = str(note or "").strip()
    if not note:
        return remove_table(text, table)
    fresh = relocate_table(text, table)
    if fresh is None:
        return str(text or "")
    lines = str(text or "").splitlines()
    start, end = fresh.start, fresh.end
    while end < len(lines) and not lines[end].strip():
        end += 1
    lead = _lead_in_index(lines, start)
    cut = lead if lead is not None and all(
        not lines[index].strip() for index in range(lead + 1, start)) else start
    return _splice(lines, cut, end, [note])


def _empty_table_note(header: Sequence[str]) -> str:
    """空表体换成的中性说明：分工/节点类表头说「分工」，其余说「内容」。"""
    header_text = " ".join(str(cell or "") for cell in header)
    return EMPTY_TABLE_NOTE if _SPLIT_HINT.search(header_text) else EMPTY_TABLE_NOTE_GENERIC


def _nearest_heading(lines: Sequence[str], start: int, *, lookback: int = 6) -> str:
    """表格上方最近的一行小节标题（最多往回看 `lookback` 个非空行），找不到返回空串。

    用来判断「本节」是哪一节：拿这个标题去招标原文找同名编号清单，找得到才敢按条目补空表。
    """
    seen = 0
    for index in range(start - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped:
            continue
        seen += 1
        if seen > lookback:
            break
        if _is_pipe_row(stripped):
            continue
        candidate = stripped.lstrip("#").strip()
        if candidate and (_HEADING.match(candidate) or _TECH_HINT.search(candidate)):
            return candidate
    return ""


def remove_table(text: str, table: Table) -> str:
    """删掉一张表（连同表后的空行），多余空行收成一个。

    表格前面的正文与空行保留：删表处仍有段落分隔，引出句不会与后文粘在一起。
    删除前在**当前文本**里重新定位并断言删除区间只覆盖表格行（`relocate_table`）：
    定不到、或区间里混进了非表格行，就一个字都不删（只报），绝不按别处算出的行号切片。
    """
    fresh = relocate_table(text, table)
    if fresh is None:
        return str(text or "")
    lines = str(text or "").splitlines()
    end = fresh.end
    while end < len(lines) and not lines[end].strip():
        end += 1
    return _splice(lines, fresh.start, end)


def _render_row(header: Sequence[str], number: int, name: str) -> str:
    """补出来的一行：序号/条目名照招标原文，其余列留占位，不编造响应内容。

    条目名**照原文逐字搬运**，不截断：旧实现 `name[:60]` 把两条长标准名抄成了半截
    （v9 实测「序号 15、22 的标准名没抄完」），成稿里就是一句残名。
    """
    cells: list[str] = []
    used_name = False
    for head in header:
        label = str(head or "").strip()
        if _INDEX_COL.search(label):
            cells.append(str(number))
        elif not used_name and _NAME_COL.search(label):
            cells.append(str(name))
            used_name = True
        elif _DEVIATION_COL.search(label):
            cells.append("{{待填:偏离说明}}")
        elif _RESPONSE_COL.search(label):
            cells.append(PLACEHOLDER)
        else:
            cells.append("{{待填:%s}}" % (label or "内容"))
    if not used_name and len(cells) > 1:
        cells[1] = str(name)
    return "| " + " | ".join(cells) + " |"


def pad_table(text: str, table: Table, items: Sequence[str]) -> str:
    """按招标原文条目名把缺的行补到表尾（值留占位）。

    补行同样是「按行号插入」：先在当前文本里重定位表格，定不到就原样返回 —— 否则行号漂移
    会把补齐的行插进表外正文中间。

    补出来的序号 = **原文条目自己的序号**（`fresh.row_count + 1` 起的连续编号）：只有在
    「表内序号本来就是 1..n 的连续前缀」时才允许补（判据在 `_pad_blockers`，调用方
    `reconcile_response_tables` 已经先验过），所以它与原文序号逐条相同，不构成重排。
    已有行一个字都不改（补行只追加）。
    """
    fresh = relocate_table(text, table)
    if fresh is None:
        return str(text or "")
    lines = str(text or "").splitlines()
    missing = [str(item) for item in items[fresh.row_count:]]
    if not missing:
        return str(text or "")
    start_number = fresh.row_count + 1
    added = [_render_row(fresh.header, start_number + offset, name)
             for offset, name in enumerate(missing)]
    lines[fresh.end:fresh.end] = added
    return "\n".join(lines)


@dataclass(frozen=True)
class _Edit:
    """一张表的处置：怎么做 + 需要的材料（补的条目 / 换的说明）。"""

    table: Table
    action: str                      # delete 删表 / pad 补条目 / note 空表换说明
    items: tuple[str, ...] = ()
    drop_lead: bool = False          # 删表时连上方那句引导语一起删（残表专用）
    empty: bool = False              # 补的是空表体（0 行），说明文案不同
    note: str = ""

    def detail(self, expected: int) -> str:
        if self.action == "delete":
            if self.drop_lead:
                return "删除残表（%d 行，同章或后文已有完整对照表）" % self.table.row_count
            return "删除半截对照表（%d 行，同章或后文已有完整表）" % self.table.row_count
        if self.action == "note":
            return "空表体换成一句说明（未能从招标原文取到本节条目）"
        if self.empty:
            return "空表体按招标原文条目补齐（0 → %d 行）" % len(self.items)
        return "补齐对照表条目（%d → %d 行）" % (self.table.row_count, len(self.items) or expected)


def _empty_table_fill(table: Table, content: str, tender_text: str,
                      candidates: Sequence[tuple[str, list[str]]]) -> list[str]:
    """空表体要补的条目清单：对照/响应表用**同一主题**的技术参数清单，其它表用「本节」编号清单。

    其它表（如供货范围责任分工）不能拿技术参数清单硬凑 —— 那会把「支护高度」填进
    「阶段/工作内容」列。只在招标原文里真能找到同名小节的编号清单时才补。
    对照/响应表同样不许跨节：条目清单必须与这张表的表头同一主题（`_subject_items`），
    对不上就返回空（调用方改成一句中性说明），绝不把别节的清单灌进来。
    """
    if is_response_table(table.header):
        return _subject_items(candidates, table)
    heading = _nearest_heading(str(content or "").splitlines(), table.start)
    return section_requirement_items(tender_text, hint=heading)


def separate_glued_tables(text: str) -> tuple[str, list[str]]:
    """把「两块表粘在一起」的正文用空行分开，返回 `(新正文, 说明)`。

    为什么（v9 事故 A 的可见形态）：`iter_tables` 已经能把这种块解析成两张表（判据见
    `_split_block`），但成稿是 Markdown —— 连续的 `|` 行在导出时会被合成**一张** Word
    表格：解析对了，成品还是串的。所以在第二个表头行前补一个空行，让它们成为两张表。
    幂等：分开之后再跑找不到切点，原文一个字不变。只补空行，不动任何一行表格内容。
    """
    lines = str(text or "").splitlines()
    notes: list[str] = []
    out: list[str] = []
    index = 0
    while index < len(lines):
        if not _is_pipe_row(lines[index]):
            out.append(lines[index])
            index += 1
            continue
        start = index
        while index < len(lines) and _is_pipe_row(lines[index]):
            index += 1
        bounds = _split_block(lines, list(range(start, index)))
        for order, (begin, end) in enumerate(bounds):
            if order:
                out.append("")
                label = " | ".join(str(cell) for cell in _cells(lines[begin])[:4])
                notes.append("在第二张表的表头「%s」前补空行：它属于另一张表，不许并进上一张"
                             % label[:24])
            out.extend(lines[begin:end])
    if not notes:
        return str(text or ""), []
    return "\n".join(out), notes


def reconcile_response_tables(chapters: Iterable[dict[str, Any]], tender_text: str,
                              *, hint: str = "") -> list[dict[str, Any]]:
    """整册核对「对照/响应」表与空表体：返回需要改写的章节（纯函数，不写库）。

    每个返回项：`chapter_index` / `chapter_id` / `title` / `action`（delete 删半截/残表 /
    pad 补齐条目 / note 空表换说明 / mixed 多种都有）/ `rows_before` / `expected`
    （招标原文条目数）/ `content`（改写后的整章正文）/ `detail`（给人看的中文说明）。

    `chapters` 需按正文顺序传入；只返回真的需要改的章节。空表体的处置不依赖招标条目清单
    （取不到条目就换中性说明），所以 `expected < MIN_EXPECTED` 时不再整册放过。
    """
    rows = [chapter for chapter in (chapters or []) if isinstance(chapter, dict)]
    if not rows:
        return []
    items = tender_requirement_items(tender_text, hint=hint)
    expected = len(items)
    # 原文所有「像逐条清单」的候选（上下文标题 + 条目名）：补行前要按表头认主题，
    # 只允许补同一主题的清单（见 `_subject_items`，禁止跨节补行）。
    candidates = _requirement_candidates(tender_text)
    # 招标原文没有逐条清单（< MIN_EXPECTED 条）时判不了半截表，只做空表体处置
    can_judge = expected >= MIN_EXPECTED
    contents = [str(chapter.get("content") or "") for chapter in rows]
    # 先把「两块表粘在一起」的正文用空行分开（见 `separate_glued_tables`）：不分开的话，
    # 导出时两张表会渲染成一张，后面的补行/删行也都落在错的表上。
    split_notes: list[list[str]] = []
    for index, content in enumerate(contents):
        separated, notes = separate_glued_tables(content)
        if notes:
            contents[index] = separated
        split_notes.append(notes)
    parsed = [iter_tables(content) for content in contents]
    responses = [[table for table in tables if is_response_table(table.header)]
                 for tables in parsed]
    blanks = [[table for table in tables if _is_empty_body(table)] for tables in parsed]
    edits: list[dict[str, Any]] = []
    for index, chapter in enumerate(rows):
        pending: list[_Edit] = []
        skipped: list[str] = []
        horizon = responses[index] if can_judge else []
        for position, table in enumerate(horizon):
            if table.row_count == 0:
                continue  # 空表体由下面的 blanks 处置，判据不同（不要求表头含对照/响应）
            # 补行/删表都必须先认主题：表头与原文候选清单对不上，就不许拿那组条目作判据
            # （v9 事故 A：1.1 采购对象表因为一列「技术要求」写的是标准号，被全书最长的
            # 第五章第四节 20 条标准判成「原文前缀」，按序号 4~23 灌了进来）。
            subject, blocked = _pad_blockers(candidates, table, hint=hint)
            if not subject:
                skipped.append(blocked)
                continue
            if _is_leading_residue(table, position) \
                    and _has_complete_copy(responses, index, position, table, len(subject)):
                # v7：导言只剩 1 行的残表，后文另有完整表 → 删表 + 删引导句
                pending.append(_Edit(table, "delete", drop_lead=True))
                continue
            if _is_excerpt(table, subject):
                # 概要表/摘自表：规模远小于原文且不从第 1 条起，本来就不是逐条列全，不动
                continue
            if not _is_incomplete(table, subject):
                # 已完整（行数 ≥ N）、单行且对不上原文、或条目与原文对不上（不是半截表）→ 不动
                continue
            if _has_complete_copy(responses, index, position, table, len(subject)):
                pending.append(_Edit(table, "delete"))
            elif blocked:
                # 序号跳号/要补的条目已在表内：一个字都不补、也不重排，交人工
                skipped.append(blocked)
            else:
                pending.append(_Edit(table, "pad", items=tuple(subject)))
        for table in blanks[index]:
            fill = _empty_table_fill(table, contents[index], tender_text, candidates)
            if fill:
                pending.append(_Edit(table, "pad", items=tuple(fill), empty=True))
            else:
                pending.append(_Edit(table, "note", note=_empty_table_note(table.header)))
        if not pending and not split_notes[index]:
            continue
        if not pending:
            # 只有「两张表粘在一起」需要分开：照样落盘，否则导出时又并成一张
            edits.append({
                "chapter_index": index,
                "chapter_id": str(chapter.get("id") or ""),
                "title": str(chapter.get("title") or ""),
                "action": "split",
                "rows_before": 0,
                "expected": expected,
                "skipped": 0,
                "content": contents[index],
                "detail": "；".join(split_notes[index]),
            })
            continue
        content = contents[index]
        # 从后往前改：后面的表先动，前面表的行号不会因为插入行而失效。
        # 顺序正确还不够 —— 每次动手前都要在**当前文本**里重新定位表格并断言区间只覆盖
        # 表格行（`relocate_table`）：删除会收掉切口处的空行，行号照样会漂。定不到的表
        # 一律不动、只报，宁可交人工也不按别处算出的行号切片。
        applied: list[_Edit] = []
        unprovable: list[_Edit] = []
        for edit in sorted(pending, key=lambda item: item.table.start, reverse=True):
            fresh = relocate_table(content, edit.table)
            if fresh is None:
                unprovable.append(edit)
                continue
            if edit.action == "delete":
                content = (remove_table_with_lead_in(content, fresh) if edit.drop_lead
                           else remove_table(content, fresh))
            elif edit.action == "note":
                content = replace_table_with_note(content, fresh, edit.note)
            else:
                content = pad_table(content, fresh, edit.items)
            applied.append(edit)
        if not applied:
            # 一张都定不到（正文已经不是解析时那一份）：一个字都不动，交人工
            continue
        actions = {edit.action for edit in applied}
        detail = "、".join(edit.detail(expected) for edit in applied)
        if split_notes[index]:
            actions.add("split")
            detail = "；".join(split_notes[index]) + "；" + detail
        if unprovable:
            detail += "；另有 %d 张表无法在当前正文里重新定位，未改动（交人工复核）" % len(unprovable)
        if skipped:
            detail += ("；另有 %d 张表按「不跨节补行 / 序号不重排」原则未改动（%s）"
                       % (len(skipped), "；".join(dict.fromkeys(skipped))))
        edits.append({
            "chapter_index": index,
            "chapter_id": str(chapter.get("id") or ""),
            "title": str(chapter.get("title") or ""),
            "action": actions.pop() if len(actions) == 1 else "mixed",
            "rows_before": sum(edit.table.row_count for edit in applied),
            "expected": expected,
            "skipped": len(unprovable),
            "content": content,
            "detail": detail,
        })
    return edits