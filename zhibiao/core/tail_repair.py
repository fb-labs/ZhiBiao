"""收尾补全：撞到输出上限而断在半个字、半个表格行、半张表时必须补齐。

线上事故（第一份6，唐安端头支架）：8 章正文末尾是
`| 2 | 推移千斤顶 | 推溜力/移架力 | 1309/824（P`、`| 出厂`、`质检组按GB/T`、
`三节之间为递` —— 明显是模型在 max_tokens 上被切断，而续写循环只按「字数够不够」
判断是否需要继续，于是半截话直接落库、进了导出文件。

线上事故（公开招标《招标文件2.11开标(1).pdf》成稿 v9，**同一份稿子里 6 处**）：
1. 第 6 章导言第 3 条只写了四个字 `3. 端尾` 就断，直接跳到 `6.1 关键设备性能及技术指标`；
2. `6.2.5 中置式大阻力端尾支架技术实现与参数响应` 表只到 `| 11 | 操纵方式 |`，
   漏 `12 整组重量` / `13 最大运输外形尺寸` / `14 最大不可拆卸件重量` 及收尾段落；
3. `7.1.6.1` 出厂例行试验表只到 `| 7 | 液控单向阀性能试验`，漏整机动作试验、电液控试验
   及 `7.1.6.2 运行现场试验`；
4. `9.1.7` 第 2 条写成 `2. 安装调试与工业性试验衔接：安装、调试、培训、工业性` —— 半句话；
5. `9.2.10` 表只到 `| 4 | 单机调试 |`，漏 `5 系统联调` / `6 工业性试验（3个月）` / `7 初验与整体验收`；
6. 全文最后 `10.2.7` 表只有 `序号 1`、`序号 2` 两行就结束。

为什么旧 `tail_is_complete` 全漏判（逐条对应上面的 6 处）：
* 第 1、4 处：末行先命中了旧的小标题正则（`^\\s*(?:#{1,6}\\s|\\d+(?:\\.\\d+){0,3}\\s*[、.．]?\\s|第X[章节])`），
  只要 `len(last) <= 40` 就直接 `return True` —— 单层编号的**列表项**（`3. 端尾`）和 `##` 标题
  被混为一谈；`2. 安装调试…工业性` 这种半句话同样被当成标题放过；
* 第 2、3、5、6 处：末行是**列数齐全、以 `|` 结尾的表格行**，`_table_row_complete` 判真、
  `_table_last_row_truncated` 只查「列数 < 表头列数」和「整行都是分隔符」，于是「表只写了一半」
  这种**行内无缺陷**的残表一律放过；
* 另外 `{{待填` 没闭合时旧逻辑会落到 `len(last) < 10` 的兜底，短占位行同样漏判。

本模块只做四件事（判据全部是通用结构量，不绑定任何设备名/表名/条款名）：
1. 判断一章正文的末尾是否完整 —— 四类未完成见 `tail_problem`：
   a. 末行是表格行，但**表体明显不足**：表头在、表体只剩 1 行；或表内最后序号 < 原文同表行数。
      表体正好 2 行时不单凭结构判残（两行对照表是正常写法），只在「被截断 / 原文还有更多行」
      有证据时才判残；
   b. 末行以 `{{待填` 结尾（占位没闭合）；
   c. 末行是**半句话**（无句末标点 `。！？；…`、不短于 10 字、不以 `：`/表格行/标题行收尾）；
      编号条目只写了「编号 + 名词」（`3. 端尾`）同样算未完成；
   d. 表格**断在中途**（表内最后序号 < 原文同表最大序号；或本次调用 finish_reason=length）。
2. 取出「断点前的最后一个句子 / 最后一个表格行」作为续写起点；
3. 给出续写提示词，让模型**接着断点往下写**（而不是用一句通用收尾句把尾巴盖住）；
4. 续写不可用时只追加一行中性说明（`（本节续写未完成，请人工补齐：…）`），
   **绝不编造正文**，也**绝不用「综上所述」这类通用收尾句伪造完整性**；
   正文类（prose / 编号条目）断尾补齐失败时**正文一个字都不改**，只由
   `incomplete_source_note` 给出记进 `chapter_source` 的事实说明（`「（收尾未完成：…）」`
   是既有的降级口径，这里复用同一套记录方式，不另造一套）。

全部判据与追加动作都是幂等的：已完整的正文再跑一次不改动，已加过说明的不重复追加。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# 这些字结尾读起来是完整的（数量、单位、序数），不算半截
_ENUM_ENDINGS = set("了等项条根件套台组个名天月日次级元吨米秒分号型款批次年月期")
# 这些结尾一定是没写完
_DANGLING = set("，,、；;：:（(【[《〈—～-·和与及或按为的对由须应将在把被从向以于其该要是并未并")

TABLE_ROW = re.compile(r"^\s*\|")
_PLACEHOLDER = re.compile(r"^\{\{待填[:：][^}]+\}\}$")
# 占位符没闭合（`…{{待填:投标人名称` / `…{{待填`）：这是被截断，不是「留给人工填」
_PLACEHOLDER_OPEN = re.compile(r"\{\{待填(?:[:：][^}]*)?$")
# 小节级编号标题：`## 6.1 …`、`第 6 章 …`、`6.1.2 标题`（多级编号，且编号后不跟列表顿号/句点）
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_CHAPTER_HEADING = re.compile(r"^\s*(?:第\s*[一二三四五六七八九十百零〇\d]{1,4}\s*[章节部分篇]|附件\s*[\d一二三四五六七八九十])")
_NUMBERED = re.compile(r"^\s*(?P<num>\d{1,3}(?:\.\d{1,3})*)\s*(?P<sep>[、.．)）:：]?)\s*(?P<rest>.*)$")
# 明确以「引导下文」结尾的写法，不算没写完
_LEAD_IN_END = "：:"
# 句末标点：出现这些才算「读完了」
_SENTENCE_END = "。！？；…"
# 表格：表体只有 1 行就认为「只写了个开头」（成稿 v9 最后一节 10.2.7 只有序号 1、2 两行）。
# 为什么阈值是 1 而不是 2：只有 2 行的对照表本身也可能是**正常写法**（v8 就有一张两行的
# 「关键部件对照」表作为整章收尾，已被 test_v8_completeness 钉住）。表体 =2 行时必须有
# 额外证据才判残：本次调用 finish_reason=length（`truncated`），或原文同表还有更多行。
MIN_TABLE_BODY = 1
# 序号列的表头字样（决定这张表是不是「逐条清单」）
_SEQ_HEADER = re.compile(r"序\s*号|编\s*号|^no\.?$|^№$", re.I)
_SEPARATOR = re.compile(r"^\s*\|[\s:|\-]*-{2,}[\s:|\-]*\|?\s*$")
# 续写未完成的标记行（幂等识别 + 质量门禁豁免）
NOTE_PREFIX = "（本节续写未完成"
NOTE_RE = re.compile(r"^\s*（本节续写未完成[^）]*）?\s*$")


def _balanced(text: str) -> bool:
    pairs = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"), ("《", "》"))
    return all(text.count(left) == text.count(right) for left, right in pairs)


def _table_row_complete(line: str) -> bool:
    stripped = line.strip()
    if not stripped.endswith("|"):
        return False
    if not _balanced(stripped):
        return False
    # 分隔行 |---|---| 也算完整
    cells = [c for c in stripped.strip("|").split("|")]
    return len(cells) >= 2


def table_header_of(text: str, line_index: int) -> str:
    """返回 line_index 所在表格块的表头行（表格第一行）；不是表格则返回空串。"""
    lines = str(text or "").splitlines()
    if not 0 <= line_index < len(lines):
        return ""
    index = line_index
    while index > 0 and TABLE_ROW.match(lines[index - 1]):
        index -= 1
    return lines[index].strip() if index < line_index else ""


def _cells(line: str) -> list[str]:
    return [cell for cell in line.strip().strip("|").split("|")]


def _table_last_row_truncated(text: str) -> bool:
    """最后一行表格行（连同它所在的表格）是否被写残。

    线上事故（第二个标书v2，4.1.3）：最后一行 `| 11 | 中间部 | … | 无偏离 |` 以 `|`
    结尾、括号也闭合，旧判定认为「表格行完整」，于是 6 列表格只剩 5 格的事直接落库，
    导出后表现为「作用域列错位 + 证明材料来源整列丢失」。
    """
    lines = str(text or "").splitlines()
    last = len(lines) - 1
    while last >= 0 and not lines[last].strip():
        last -= 1
    if last < 0 or not TABLE_ROW.match(lines[last]):
        return False
    last_cells = _cells(lines[last])
    header = table_header_of(text, last)
    header_cells = _cells(header) if header else []
    if len(header_cells) >= 3 and len(last_cells) < len(header_cells):
        return True
    # 表格块只剩表头/分隔行（| a | b |\n|---|---|）也是残表
    return not any(not re.fullmatch(r"[\s:|-]+", cell or "") for cell in last_cells)


def _is_separator(line: str) -> bool:
    return bool(_SEPARATOR.match(str(line or "")))


def _row_seq(line: str) -> int | None:
    """表格行的第一个单元格是纯序号时返回该序号，否则 None。"""
    cells = _cells(line)
    if not cells:
        return None
    match = re.fullmatch(r"\s*(\d{1,3})\s*[.．、]?\s*", cells[0] or "")
    return int(match.group(1)) if match else None


def _last_table_block(text: str) -> dict[str, Any]:
    """章末那张表：表头、表体行（去分隔行）、所在行范围；章末不是表格行时返回 {}。"""
    lines = str(text or "").splitlines()
    index = len(lines) - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0 or not TABLE_ROW.match(lines[index]):
        return {}
    start = index
    while start > 0 and TABLE_ROW.match(lines[start - 1]):
        start -= 1
    end = index
    while end + 1 < len(lines) and TABLE_ROW.match(lines[end + 1]):
        end += 1
    block = [lines[at] for at in range(start, end + 1)]
    header = block[0].strip()
    body = [row.strip() for row in block[1:] if row.strip() and not _is_separator(row)]
    return {"header": header, "body": body, "start_index": start, "row_index": index}


def _looks_numbered(header: str, body: Sequence[str]) -> bool:
    """这张表是不是「逐条清单」（有序号列，或表体第一列正好是 1..N）。"""
    if any(_SEQ_HEADER.search(cell) for cell in _cells(header)):
        return True
    seqs = [_row_seq(row) for row in body]
    known = [seq for seq in seqs if seq is not None]
    return bool(known) and len(known) == len(body) and known == list(range(1, len(known) + 1))


def _bigrams(text: str) -> set[str]:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text or ""))
    return {clean[index:index + 2] for index in range(len(clean) - 1)}


def _overlap(left: str, right: str) -> int:
    return len(_bigrams(left) & _bigrams(right))


def source_row_expectation(header: str, body: Sequence[str],
                           source_items: Sequence[str]) -> tuple[int | None, list[str]]:
    """本章末表若正好是招标原文同表的**前若干行**，返回 (原文应有行数, 缺少的条目名)。

    第 2、3、5 处的残表在正文里没有任何结构缺陷（列数齐全、以 `|` 结尾），唯一可靠的证据
    是「原文这张表还有后面几行」。判定必须保守：只有正文表格逐行都能与原文条目对上
    （首行 ≥2 个二元组重合，且过半行 ≥2 个重合），才认为它是原文同表的前缀；
    对不上（摘要表、概览表、别的表）一律返回 (None, [])，绝不据此改写。
    """
    items = [str(item).strip() for item in (source_items or ()) if str(item).strip()]
    if len(items) < 3 or not body:
        return None, []
    rows = [_cells(row) for row in body]
    if len(rows) > len(items):
        return None, []
    first = max((_overlap(cell, items[0]) for cell in rows[0]), default=0)
    if first < 2:
        return None, []
    matched = 0
    for position, cells in enumerate(rows):
        best = max((_overlap(cell, items[position]) for cell in cells), default=0)
        if best >= 2:
            matched += 1
    if matched * 2 < len(rows):
        return None, []
    return len(items), items[len(rows):]


def _placeholder_unclosed(line: str) -> bool:
    return bool(_PLACEHOLDER_OPEN.search(str(line or "").strip()))


def _note_line(line: str) -> bool:
    return bool(NOTE_RE.match(str(line or "").strip()))


def tail_has_note(text: str) -> bool:
    """章末是否已经有「续写未完成」的中性说明（幂等用）。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return bool(lines) and _note_line(lines[-1])


def _prose_incomplete(line: str) -> tuple[bool, str]:
    """一行正文是不是没写完；返回 (是否未完成, 原因)。

    口径（与旧 `tail_is_complete` 的兜底保持一致，只加不减）：
    * 句末标点 / 引号括号收尾 → 完整；
    * 以 `：` 收尾 → 完整（冒号是「引导下一段」的正常写法）；
    * 悬空连接词/标点、没闭合的 `{{待填` → 未完成；
    * 数量/单位/ASCII 单位收尾 → 完整；
    * ASCII 字母/斜杠收尾（`按GB/T`）→ 未完成；
    * 其余不短于 10 字的普通汉字结尾 → 未完成（实测「三节之间为递」「检查进度偏差」）。
      阈值取 10 而不是 12：旧判据的兜底就是 10，收紧到 12 会把「关键外购件按合格供应商」
      （11 字）这类已确证的截断放过去；12 字的要求已被这条完全覆盖。
    """
    stripped = str(line or "").strip()
    if not stripped:
        return False, ""
    if stripped[-1] in _SENTENCE_END:
        return False, ""
    if stripped[-1] in "”』」】）)]":
        return False, ""
    # 平台自己的成对标记（`{{图片占位:…}}`、`{{待填:…}}`、`[TABLE]`）：整行是标记，不是半句
    if stripped.startswith(("[TABLE]", "[/TABLE]")):
        return False, ""
    if stripped.startswith("{{") and stripped.endswith("}}"):
        return False, ""
    if stripped[-1] in _LEAD_IN_END:
        return False, ""
    if _placeholder_unclosed(stripped):
        return True, "placeholder-unclosed"
    if stripped[-1] in _DANGLING:
        return True, "dangling"
    if stripped[-1] in _ENUM_ENDINGS:
        return False, ""
    # A measurement ending at a bare number may have lost its unit when the
    # model stopped mid-sentence (e.g. "泵站压力31.5").  Keep the original
    # text and ask the existing continuation path to complete the fragment.
    if re.search(r"(?:压力|阻力|初撑力|强度|比压|功率|流量|尺寸|重量|温度)\s*[:：=为约]?\s*\d+(?:\.\d+)?$", stripped):
        return True, "measurement-unit-missing"
    if re.search(r"(?:mm|kN|MPa|kW|kg|t|m|%|\d)$", stripped):
        return False, ""
    if re.search(r"[A-Za-z/\\-]$", stripped):
        return True, "ascii-cut"
    if len(stripped) >= 10:
        return True, "half-sentence"
    return False, ""


def _numbered_fragment(line: str) -> tuple[bool, str]:
    """编号行是「标题行」还是「没写完的条目」；返回 (是否未完成, 原因)。

    关键区分（第 1 处事故的根因就在这）：
    * 多级编号（`6.1` / `6.1.2`）且编号后不跟 `、．)` → 小节标题行，完整；
    * `## …` / `第 X 章` → 标题行，完整；
    * 单层编号后跟 `.`/`、`/`)`（`3. 端尾`）→ **列表条目**，只有编号+名词就算没写完；
    * 条目正文以 `：` 结尾 → 完整（引导下文）；以句末标点结尾 → 完整。
    """
    stripped = str(line or "").strip()
    match = _NUMBERED.match(stripped)
    if not match:
        return False, ""
    number = match.group("num") or ""
    sep = match.group("sep") or ""
    rest = (match.group("rest") or "").strip()
    if "." in number and not sep:
        return False, ""          # 6.1 / 6.1.2：小节标题
    if not sep and "." not in number:
        return False, ""          # `3 端尾`：编号后没标点，按标题处理（证据不足不改）
    if not rest:
        return True, "list-item-empty"
    incomplete, reason = _prose_incomplete(rest)
    if incomplete:
        return True, "list-item-fragment"
    # 条目只写了「编号 + 名词」（`3. 端尾`）：读起来没有落点，后面的条目正文没写出来。
    # 只认「读到完整」的几种收尾（句末标点 / 冒号引导 / 数量单位 / 引号括号），其余一律算残。
    if (rest[-1] not in _SENTENCE_END and rest[-1] not in _LEAD_IN_END
            and rest[-1] not in "”』」】）)]" and rest[-1] not in _ENUM_ENDINGS
            and not re.search(r"(?:mm|kN|MPa|kW|kg|t|m|%|\d)$", rest)):
        return True, "list-item-fragment"
    return False, reason


def _problem(kind: str, mode: str, reason: str, text: str, unit: str, *,
             header: str = "", last_seq: int | None = None,
             missing_rows: int | None = None, source_rows: Sequence[str] = (),
             detail: str = "") -> dict[str, Any]:
    _kind, start, _unit = last_unit(text)
    return {
        "kind": kind, "mode": mode, "reason": reason, "start": int(start),
        "unit": str(unit or ""), "header": str(header or ""), "last_seq": last_seq,
        "missing_rows": missing_rows, "source_rows": list(source_rows or ()),
        "detail": str(detail or ""),
    }


def _table_tail_problem(text: str, *, expected_rows: int | None = None,
                        source_items: Sequence[str] = (), truncated: bool = False,
                        row_index: int | None = None) -> dict[str, Any] | None:
    block = _last_table_block(text)
    if not block:
        return None
    lines = str(text or "").splitlines()
    index = block["row_index"] if row_index is None else row_index
    last_line = lines[index].strip() if 0 <= index < len(lines) else ""
    body = list(block["body"])
    header = block["header"]
    last_row = body[-1] if body else last_line
    seqs = [seq for seq in (_row_seq(row) for row in body) if seq is not None]
    last_seq = seqs[-1] if seqs else None
    numbered = _looks_numbered(header, body)

    if not _table_row_complete(last_line):
        return _problem("table", "replace", "row-broken", text, last_line, header=header,
                        last_seq=last_seq, detail="最后一行表格没有闭合")
    if _placeholder_unclosed(last_line):
        return _problem("table", "replace", "row-placeholder", text, last_line, header=header,
                        last_seq=last_seq, detail="最后一行表格里的待填占位没闭合")
    # 只有表头（或表头+分隔行），一行数据都没有：这一定是写到一半断了。
    # 注意 table[0] 自己是数字序号时不算「表头」——那是一张表被甩出来的单行残片，
    # 旧判据（`test_closed_table_row_is_complete`）已把这种单行视为完整。
    if not body and _row_seq(header) is None:
        return _problem("table", "append", "table-body-empty", text, header, header=header,
                        detail="表格只有表头，没有任何表体行")
    if _table_last_row_truncated(text):
        return _problem("table", "replace", "row-columns", text, last_line, header=header,
                        last_seq=last_seq, detail="最后一行表格的列数与表头不一致")

    expected, missing_items = source_row_expectation(header, body, source_items)
    if expected is None and expected_rows and expected_rows > len(body):
        expected = int(expected_rows)
        missing_items = []
    if expected and len(body) < expected:
        reason = ("table-seq-short"
                  if (last_seq is not None and last_seq < expected) else "table-rows-short")
        return _problem("table", "append", reason, text, last_row, header=header,
                        last_seq=last_seq, missing_rows=expected - len(body),
                        source_rows=missing_items,
                        detail="表内最后序号 %s，原文同表 %d 行" % (last_seq, expected))

    if numbered and len(body) <= MIN_TABLE_BODY:
        return _problem("table", "append", "table-body-short", text, last_row, header=header,
                        last_seq=last_seq,
                        detail="表头在、表体只有 %d 行" % len(body))

    if truncated:
        return _problem("table", "append", "table-cut-by-length", text, last_row,
                        header=header, last_seq=last_seq,
                        detail="本次调用被 max_tokens 截断，且章节停在表格行上")
    return None


def tail_problem(text: str, *, expected_rows: int | None = None,
                 source_items: Sequence[str] = (), truncated: bool = False) -> dict[str, Any] | None:
    """本章正文末尾是否未完成；未完成时返回断点信息，否则返回 None。

    四类判据（全部是通用结构量，不认任何具体设备/表名/条款名）：
    a. 末行是表格行且表体明显不足：表头存在、表体只剩 1 行；
       或表内最后序号 < 原文同表行数（`expected_rows` / `source_items` 由调用方从招标原文取）。
       表体正好 2 行时不算残表 —— 两行的对照表本身就是正常写法（v8 实测有一张）；
       两行表只在「本次调用被截断 / 原文同表还有更多行」有证据时才算残；
    b. 末行以 `{{待填` 结尾（占位没闭合）；
    c. 末行是半句话：无句末标点、不短于 10 字、不以 `：`/表格行/标题行收尾；
       编号条目只写了「编号 + 名词」（`3. 端尾`）同样算未完成；
    d. 表格断在中途（表内最后序号 < 原文同表最大序号），或本次调用 finish_reason=length
       且章末停在表格行上（`truncated=True`）。

    返回的 `mode` 决定后续动作：`replace` 只重写断点前最后一句/最后一行；
    `append` 从断点继续往下写（表格缺行只能续写，替换最后一行补不回后面的行）。
    """
    body = str(text or "")
    raw = body.splitlines()
    index = len(raw) - 1
    while index >= 0 and not raw[index].strip():
        index -= 1
    if index < 0:
        return None
    last = raw[index].strip()
    if last.startswith(("```", "~~~")):
        return None
    if _note_line(last):
        return None
    if TABLE_ROW.match(last):
        return _table_tail_problem(body, expected_rows=expected_rows,
                                   source_items=source_items, truncated=truncated,
                                   row_index=index)
    if _PLACEHOLDER.match(last):
        return None
    if _placeholder_unclosed(last):
        return _problem("prose", "replace", "placeholder-unclosed", body, last,
                        detail="待填占位没闭合")
    if _MARKDOWN_HEADING.match(last) or _CHAPTER_HEADING.match(last):
        return None
    if _NUMBERED.match(last):
        incomplete, reason = _numbered_fragment(last)
        if not incomplete:
            return None
        return _problem("list", "replace", reason, body, last,
                        detail="编号条目只写了编号与名词" if reason.startswith("list-item") else "")
    incomplete, reason = _prose_incomplete(last)
    if not incomplete:
        return None
    return _problem("prose", "replace", reason, body, last)


def tail_is_complete(text: str, *, expected_rows: int | None = None,
                     source_items: Sequence[str] = (), truncated: bool = False) -> bool:
    """正文末尾是否完整。空正文视为完整（由调用方按其它规则处理）。"""
    return tail_problem(text, expected_rows=expected_rows,
                        source_items=source_items, truncated=truncated) is None


def last_unit(text: str) -> tuple[str, int, str]:
    """返回 (类型, 起始下标, 单元文本)；类型为 'table' 或 'prose'。"""
    body = str(text or "").rstrip("\n")
    if not body:
        return "prose", 0, ""
    line_start = body.rfind("\n") + 1
    last_line = body[line_start:].strip()
    if TABLE_ROW.match(last_line):
        return "table", line_start, last_line
    # 句子：从最后一个句末标点之后开始
    cut = max(body.rfind(ch) for ch in "。！？；…\n")
    start = cut + 1 if cut >= 0 else 0
    return "prose", start, body[start:].strip()


def last_table_header(text: str) -> str:
    """正文最后一行表格所在表格块的表头（用于补全时对齐列数）。"""
    lines = str(text or "").splitlines()
    last = len(lines) - 1
    while last >= 0 and not lines[last].strip():
        last -= 1
    return table_header_of(text, last)


def tail_repair_prompt(title: str, kind: str, unit: str, header: str = "") -> str:
    target = "表格行" if kind == "table" else "句子"
    rule = ("表格行必须用 | 分隔各列、以 | 结尾，列数与本章前面的表头一致"
            if kind == "table" else "保持原有的句式与标点，以句号结尾")
    context = f"本章表头：{header}\n" if (kind == "table" and header) else ""
    return (
        f"你是投标文件校对员。下面是一章正文的最后一个{target}，它因为输出长度上限被截断了。\n"
        f"请只输出**补全后的这一个{target}**：不要解释、不要重复前文、不要新起标题，{rule}。\n"
        f"如果它其实已经完整，请只输出 <OK>。\n\n"
        f"章节标题：{title}\n"
        f"{context}"
        f"被截断的{target}：{unit}\n"
    )


def tail_continue_prompt(title: str, problem: dict[str, Any],
                         source_rows: Sequence[str] = ()) -> str:
    """「从断点接着写」的提示词：续写表格缺行 / 补完编号条目，再补一段收尾说明。

    与 `tail_repair_prompt`（只重写一个单元）的区别：表格断在中途时，缺的是**后面的几行**，
    替换最后一行补不回来；编号条目只写了名词时，缺的是这一条的正文与后续各条。
    这时必须让模型从断点往后写，并且明确「不许重复已有内容、不许新起标题」。
    """
    kind = str(problem.get("kind") or "prose")
    unit = str(problem.get("unit") or "")
    header = str(problem.get("header") or "")
    last_seq = problem.get("last_seq")
    rows = [str(item).strip() for item in (source_rows or problem.get("source_rows") or ())
            if str(item).strip()]
    if kind == "table":
        head = (
            "你是投标文件校对员。下面这一章正文在输出长度上限处被截断了：最后一张表格只写到这里，"
            "后面的表格行与表后的收尾段落都没有写出来。\n"
            "请只输出**续写内容**：接着同一张表继续写剩余的表格行（序号连续、不得跳号，"
            "列数与表头一致，每行以 | 结尾），表格写完后另起一段写 1~2 句收尾说明。\n"
            "不要重复已经写过的行，不要新起标题，不要输出解释或代码块。"
            "如果本表确实已经写完，请只输出 <OK>。\n"
        )
        context = "章节标题：%s\n本章表头：%s\n" % (title, header)
        if last_seq is not None:
            context += "已写到的最后一行：%s（序号 %s，续写从序号 %d 开始）\n" % (
                unit, last_seq, int(last_seq) + 1)
        else:
            context += "已写到的最后一行：%s\n" % unit
        if rows:
            context += ("原文该表还缺以下条目，必须逐条写全、名称不得改写："
                        + "、".join(rows) + "\n")
        return head + "\n" + context
    head = (
        "你是投标文件校对员。下面这一章正文在输出长度上限处被截断了，最后一条/最后一句没写完。\n"
        "请只输出**续写内容**：从被中断的地方继续写下去，保持原有的编号体例、层级结构与句式；"
        "如果断点是一个只有编号和名词的条目，请补全这一条的正文，并接着写完后续各条。\n"
        "不要重复已经写过的内容，不要新起标题，不要输出解释。"
        "如果确实已经写完，请只输出 <OK>。\n"
    )
    return head + "\n章节标题：%s\n已写到的最后内容：%s\n" % (title, unit)


def tail_prompt_for(problem: dict[str, Any], *, title: str,
                    source_rows: Sequence[str] = ()) -> str:
    """按断点类型选提示词：单句/单行残行用替换式，表格缺行与编号条目缺正文用续写式。"""
    kind = str(problem.get("kind") or "prose")
    mode = str(problem.get("mode") or "replace")
    if kind == "table":
        if mode == "replace":
            return tail_repair_prompt(title, "table", str(problem.get("unit") or ""),
                                      str(problem.get("header") or ""))
        return tail_continue_prompt(title, problem, source_rows)
    if kind == "list":
        return tail_continue_prompt(title, problem, source_rows)
    return tail_repair_prompt(title, "prose", str(problem.get("unit") or ""))


def _clean_fragment(fragment: str) -> str:
    """去掉模型常见的包装（代码块、`续：`前缀、空行），只留正文。"""
    lines: list[str] = []
    for raw in str(fragment or "").replace("\r\n", "\n").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if lines:
                lines.append("")
            continue
        if line.strip().startswith(("```", "~~~")):
            continue
        lines.append(re.sub(r"^\s*(?:…+|（续）|续[:：]?|补充[:：]?)\s*", "", line).strip())
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _norm_row(line: str) -> str:
    return re.sub(r"\s+", "", str(line or ""))


def _append_fragment(text: str, fragment: str) -> str:
    """把续写内容接到正文末尾；重复的表格行丢掉（模型常重写最后一行）。

    两个细节都是导出口径要求：
    * 表格行之后直接接正文会被表格解析器当成「表的续行」吞掉，必须在中间留一个空行；
    * 模型爱把已经写过的最后一行再抄一遍（列数一致、只是空格不同），按去空白后的内容去重。
    """
    body = str(text or "").rstrip()
    existing = {_norm_row(row) for row in body.splitlines() if TABLE_ROW.match(row)}
    last_is_row = bool(body) and bool(TABLE_ROW.match(body.splitlines()[-1]))
    additions: list[str] = []
    for line in _clean_fragment(fragment).splitlines():
        stripped = line.strip()
        if not stripped:
            if additions and additions[-1]:
                additions.append("")      # 保留模型自己的分段
            continue
        if TABLE_ROW.match(stripped):
            key = _norm_row(stripped)
            if key in existing:
                continue
            existing.add(key)
        additions.append(stripped)
    while additions and not additions[-1]:
        additions.pop()
    if not additions:
        return body
    if last_is_row and not TABLE_ROW.match(additions[0]):
        additions.insert(0, "")
    return body + "\n" + "\n".join(additions)


def apply_tail_fragment(text: str, problem: dict[str, Any], fragment: str) -> str:
    """把模型续写/补全的结果写回正文：替换式只换断点单元，续写式追加到末尾。"""
    cleaned = _clean_fragment(fragment)
    if not cleaned:
        return str(text or "")
    if str(problem.get("mode") or "replace") == "append":
        return _append_fragment(text, cleaned)
    return merge_tail(text, int(problem.get("start") or 0), cleaned)


def merge_tail(text: str, start: int, replacement: str) -> str:
    """用补全结果替换正文末尾的修复单元，保留前面的全部内容。"""
    replacement = str(replacement or "").strip()
    if not replacement:
        return text
    # 去掉模型可能加的“…”或“续：”之类前缀
    replacement = re.sub(r"^(?:…+|（续）|续[:：]?|<OK>)\s*", "", replacement).strip()
    head = str(text or "")[:start]
    if head and not head.endswith("\n") and replacement.startswith("|"):
        # 表格行被整行替换：前面补一个换行，避免粘到上一段末尾
        return head + "\n" + replacement
    return head + replacement


def missing_note(problem: dict[str, Any]) -> str:
    """续写不可用时的中性说明：只说明「这里还缺什么」，不写任何正文、不做任何断言。"""
    rows = problem.get("missing_rows")
    if str(problem.get("kind") or "") == "table":
        if rows:
            return "%s，请人工补齐：此处原文还有 %d 行）" % (NOTE_PREFIX, int(rows))
        return "%s，请人工补齐：此处原文还有若干行，另需一段收尾说明）" % NOTE_PREFIX
    return "%s，请人工补齐：此处原文还有一段）" % NOTE_PREFIX


# 说明里引用断点原文时的字数上限：只留「停在哪」，不把整段抄进来源列。
_SOURCE_QUOTE_LIMIT = 40
# 来源列用「（）」包裹各段说明，引用里的成对括号改成半角/书名号，避免套娃歧义。
_SOURCE_QUOTE_MAP = str.maketrans({"（": "(", "）": ")", "「": "『", "」": "』",
                                   "【": "[", "】": "]", "\n": " "})


def _fact_quote(unit: str, limit: int = _SOURCE_QUOTE_LIMIT) -> str:
    """把断点原文压成一行事实引用：只去空白/换行/括号，不改一个字、不补一个字。"""
    text = re.sub(r"\s+", " ", str(unit or "")).strip()
    if len(text) > max(1, int(limit)):
        text = "…" + text[-max(1, int(limit)):]
    return text.translate(_SOURCE_QUOTE_MAP)


def incomplete_source_note(problem: dict[str, Any], *,
                           limit: int = _SOURCE_QUOTE_LIMIT) -> str:
    """续写之后仍不完整时写进 `chapter_source` 的中性说明（只记录事实，绝不补正文）。

    线上事故（项目 bee64b91…，seq=14）：续写拿不到内容时，整章 800 字正文被当成
    「生成失败」丢掉。新口径是**保留正文**，把「结尾没写完、停在哪」记在来源里，
    由人工决定补不补。因此这里只允许陈述三件事：
    * 本章结尾（或末表）没写完；
    * 原文末句/末表还缺什么（引用原文，不改写）；
    * 已按现有内容导出，建议人工补齐。
    绝不出现「综上所述」「本章到此结束」这类补出来的收尾句，也不断言内容正确。
    """
    kind = str((problem or {}).get("kind") or "prose")
    if kind == "table":
        rows = (problem or {}).get("missing_rows")
        fact = ("本章末表未写完，表内还缺 %d 行" % int(rows)) if rows else "本章末表未写完"
        return fact + "，已按现有内容导出，建议人工补齐"
    quote = _fact_quote(str((problem or {}).get("unit") or ""), limit)
    if quote:
        return "本章结尾未完，原文末句：「%s」，已按现有内容导出，建议人工补齐" % quote
    return "本章结尾未完，已按现有内容导出，建议人工补齐"


def append_incomplete_note(text: str, problem: dict[str, Any]) -> str:
    """在章末追加一行中性说明（幂等：已有说明就不再追加，也不改动正文一个字）。"""
    body = str(text or "")
    if tail_has_note(body):
        return body
    return body.rstrip() + "\n\n" + missing_note(problem)


def needs_repair(text: str, *, expected_rows: int | None = None,
                 source_items: Sequence[str] = (), truncated: bool = False) -> bool:
    """末尾异常且确实还有内容时才需要补全。"""
    body = str(text or "").strip()
    if len(body) < 20:
        return False
    return not tail_is_complete(body, expected_rows=expected_rows,
                                source_items=source_items, truncated=truncated)
