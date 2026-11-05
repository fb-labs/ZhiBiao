"""串行拼接：① 被换行拆开的表格行拼回整行 ② 段末半句补成完整句。

两件事同源：**模型输出被换行切断后，引擎没有任何「接回去」的逻辑**，于是残片直接落库。

线上实测（v7）：

* **10.2.10《招标技术要求逐条响应》**：第 1～10 条正常，第 11～15 条变成两行 ——
  第一行是 `| 11 | 机尾 | 机尾采用…半句招标原句 |`（列数不足），第二行才是
  `| {{待填:投标响应}} | {{待填:作用域/适用对象}} | {{待填:证明材料来源}} |`；
  表格底部还甩出一行**表外残片** `11 | 机尾`（没有行首竖线，正文里就是一串垃圾文字）。
  导出后表现为「左侧半句原句 + 右侧三列全占位」，评委看到的是没写完的对照表。
* **12.2.7 第 2 条**：`2. 我方在安装调试期间接受招标人及矿方的安全、质量` 后面直接换行，
  句子停在「安全、质量」——没有句末标点、也没有宾语。旧断尾判定只看**章末最后一行**
  （`chapter_quality._truncation_issue` / `tail_repair.tail_is_complete`），段中间的半句
  完全看不到。

本模块只做纯文本修复，两个函数都是纯函数、幂等、**不抛异常**（任何内部异常都退化为
「原文原样返回」），不调模型、不做 IO：

1. `join_split_table_rows(text)` —— 表格行必须**整行落在同一行文本内**，不得把一个单元格
   的内容拆到表外。判据只用结构性证据，宁可不拼也不乱拼：
   * **续行**：上一行列数不足表头，下一行首格是 `{{待填:…}}` 占位（占位符不可能是新行的
     序号）；或上一行以数字序号开头、下一行首格不是数字且两段格子数正好凑满表头列数。
     首格为空的只在「正好凑满」时认（跨行合并的小标题行不乱拼）。
   * **表外残片**：表格后面紧跟的、带行内竖线但没有句末标点的短行。上一行缺列 → 残片是
     它的后半段，拼回去；否则残片首格与表内某行序号相同、且**确实像半截短行**（去掉竖线
     ≤ 20 字、不带句读与「见/详见/如下/参见/说明」这类指代词、不含超过 8 字的连续中文）→
     它是该行开头片段的重复残片，删掉（否则导出成一行裸文本）；只要有一条不符就保留并记一笔，
     不拿正文句子当残片删。对不上任何行就原样留着，不猜。
   * 「合计/小计/备注/说明」这类行不是数据行，不会被当成续行拼上去。
2. `complete_truncated_paragraphs(text)` —— 段末半句补成完整句。**只在行尾追加，绝不在行内插入**。

   线上 v1 事故（另一份标书，5 处）：模型把一句话硬折成两行（Markdown 软换行导出后仍是一句），
   旧代码只看「这一行末没有句号 + 行内有逗号」，就把通用宾语 `等方面的监督与管理。` 拼到了
   折行的行尾。两行连读时，宾语正好落在句子中间，把词劈开：
   `…修改询比采购项目响|等方面的监督与管理。|应文件…`（「响应文件」被劈开）、
   `…执行MT/T1097-2008《煤矿|等方面的监督与管理。|设备检修技术规范》…`（标准名被劈开）、
   `…采用德国SIMRIT原|等方面的监督与管理。|原装进口氟胶油封…`。
   根因不是「插入位置」写错，而是**把折行误判成段落末尾**：在行尾追加，对段落末尾成立，
   对被硬折的半句就是行内插入。

   现在的判据（A4，同时满足才动手）：
   * a) 行末没有句末标点（。；！？）、顿号/逗号、序号、表格竖线、数字+单位、落款字样；
   * b) 行末最后一个字不是常见收尾字（的/了/者/名称/日期/金额/%）；
   * c) 行内存在完整句子结构（至少一个已闭合的 `。`，或行首编号 `1.`/`（1）`/`一、`），
     或行末是悬空连接词（与/及/和/或/对/在/为/按/根据/包括/如）；
   * d) 下一行不是同一句的续写（不以标点、编号、标题、项目符号、表格开头）——否则这行只是硬折行；
   * 含 `《》`（标准名）、英文/数字编码（MT/T1097-2008、M10~M42、SIMRIT）、`{{…}}`、表格行、
     紧邻 `:` 的标签行、委托书/授权书这类照录文书正文 → **一律只检出、不修改**；
   * **不再补通用宾语**：「等方面的监督与管理」「等方面的内容」「等要求」这类兜底已删除。
     拿不出同一句里的具体宾语就**不改**（宁可交人工，也不制造新的半句），说明里写清依据；
   * 仅保留「同一句已有依据」的收句：`接受/服从/遵守 … 安全、质量` → 补「监督与检查」
     （句中已有矿方/井下事实时补「，严格执行矿方井下作业规程」）；行末悬空连接词 → 去掉后收句。
"""
from __future__ import annotations

import re
from typing import Any

# ─────────────────────────── 通用 ───────────────────────────

_SEPARATOR = re.compile(r"^\s*\|[\s:|-]*-{2,}[\s:|-]*$")
_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+\S|\d{1,2}(?:\.\d{1,3})+\s+\S|第[一二三四五六七八九十百\d]{1,4}[章节])")
_PLACEHOLDER_CELL = re.compile(r"^\{\{待填[:：][^}]+\}\}$")
_INDEX_CELL = re.compile(r"^\d{1,3}$")
_NO_JOIN_FIRST = ("合计", "小计", "总计", "备注", "说明", "注", "合价", "总价")
_TERMINAL = "。！？；：…”』」】）)》〉"


def _cells(line: str) -> list[str]:
    stripped = str(line or "").strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_pipe_row(line: str) -> bool:
    stripped = str(line or "").strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _render(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _fit(cells: list[str], width: int) -> list[str]:
    """一行格子归一到表头列数：多的并进最后一列（不丢内容），少的补空（不留残列）。"""
    values = [str(cell) for cell in cells]
    if width <= 0 or len(values) == width:
        return values
    if len(values) > width:
        rest = [cell for cell in values[width - 1:] if cell]
        return values[:width - 1] + ["；".join(rest)]
    return values + [""] * (width - len(values))


# ═══════════════════ ① 被拆开的表格行拼回整行 ═══════════════════

def _is_row_fragment(line: str) -> bool:
    """表外残片：不像正文、却带着表格的行内竖线（`11 | 机尾`）。"""
    stripped = str(line or "").strip()
    if not stripped or stripped.startswith(("|", "#", "```", "~~~")):
        return False
    if "|" not in stripped or _SEPARATOR.match(stripped):
        return False
    if _HEADING.match(stripped) or _PLACEHOLDER_CELL.match(stripped):
        return False
    if stripped[-1] in _TERMINAL or len(stripped) > 60:
        return False
    return len(_cells(stripped)) >= 2


def _continuation_evidence(previous: list[str], following: list[str],
                           width: int) -> str:
    """下一行是不是上一行的续行；是就返回证据说明，不是返回空串。

    **一整行（列数已够表头）永远不可能是上一行的后半段**：并进去等于把两行压成一行 ——
    v9 事故 B 的「序号跳号」（端尾备件表只剩序号 2、4、6、7、8）就是这么来的：上一行
    缺列时，紧随其后的完整行（尤其首格为空、或以 `{{待填}}` 起头的那种）被当成了续行。
    续行只允许是「自己也不成行」的残片。
    """
    if not following or len(previous) >= width or len(following) >= width:
        return ""
    first = str(following[0] or "").strip()
    if _PLACEHOLDER_CELL.match(first):
        return "续行首格是 {{待填:…}} 占位，不可能是新行的序号"
    if first in _NO_JOIN_FIRST or any(first.startswith(word) for word in _NO_JOIN_FIRST):
        return ""
    if first == "":
        return ("上一行 %d 格 + 续行 %d 格 = 表头 %d 列，正好凑满"
                % (len(previous), len(following), width))
    if not _INDEX_CELL.match(str(previous[0] or "").strip()):
        return ""
    if _INDEX_CELL.match(first):
        return ""
    if len(previous) + len(following) != width:
        return ""
    return ("上一行以数字序号开头、续行首格不是数字，且 %d+%d 格正好凑满表头 %d 列"
            % (len(previous), len(following), width))


def _fragment_belongs_to(fragment: list[str], row: list[str]) -> bool:
    """这块表外残片能不能算作**这一行**的后半段：残片里没有序号，或序号与这一行相同。

    为什么（v9 事故 B）：旧实现只要末行列数不够，就把表后紧跟的所有「带竖线的短行」
    都拼到末行上。残片其实属于上一行（序号不同）时，末行被改写、残片原属的那一行
    却少了一截 —— 导出后就是「某几行的数量/规格变成占位或空」。只认序号对得上的，
    对不上就原样留着（宁可不拼，也不把两行拼串）。
    """
    if not fragment:
        return False
    head = str(fragment[0] or "").strip()
    if not _INDEX_CELL.match(head):
        return True
    return bool(row) and head == str(row[0] or "").strip()


def _trim_repeated_head(fragment: list[str], row: list[str]) -> list[str]:
    """残片开头若与这一行已有的开头**逐格相同**（重复抄了表内那几格），先去掉重复部分再拼。

    v9 实测：残片是 `7 | 销轴 | φ30 | 件 | 4` 而表内 7 号行已经有 `7 | 销轴` 时，直接
    拼接会把整行拼成 `7 | 销轴 | 7 | 销轴 | φ30；件；4`（序号列出现两次、后三格被压成一格）。
    逐格比出重复的前缀并去掉，拼出来才与原文那一行逐格相同。
    """
    overlap = 0
    for index, cell in enumerate(fragment):
        if index >= len(row):
            break
        if str(cell or "").strip() != str(row[index] or "").strip():
            break
        overlap = index + 1
    return list(fragment[overlap:]) if overlap else list(fragment)


# 删表外残片的收紧判据：残片是**半截短行**，正文句子一条都不许删。
_DEBRIS_MAX_CHARS = 20          # 去掉竖线/空白后的总字数上限
_DEBRIS_MAX_CJK_RUN = 8         # 最长连续中文串上限（正文句子远超此长度）
_DEBRIS_PUNCT = "。，；、！？"
_DEBRIS_CUE = ("详见", "参见", "处理见", "如下", "说明", "见")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _debris_rejection(fragment: str) -> str:
    """判断「序号与表内某行相同」的表外行能否当残片删掉：能删返回空串，否则返回保留理由。

    审计实测：只看「紧贴表体 + 带竖线 + 首格等于表内序号」会把 `11 | 机尾的处理见 6.3.11`
    这种正文句子删掉。收紧为三条全部满足才删：短（≤ 20 字）、无句读/指代词、无长中文短语。
    """
    body = str(fragment or "")
    if not body:
        return ""
    compact = re.sub(r"[|\s\u3000]+", "", body)
    if len(compact) > _DEBRIS_MAX_CHARS:
        return "去掉竖线后 %d 字，超过 %d 字，不像半截短行" % (len(compact), _DEBRIS_MAX_CHARS)
    marks = [mark for mark in _DEBRIS_PUNCT if mark in body]
    if marks:
        return "带句读「%s」" % "".join(marks)
    cues = [word for word in _DEBRIS_CUE if word in body]
    if cues:
        return "带指代词「%s」" % cues[0]
    longest = max((len(run) for run in _CJK_RUN.findall(body)), default=0)
    if longest > _DEBRIS_MAX_CJK_RUN:
        return "含 %d 字连续中文，像完整短语或句子" % longest
    return ""


def _rebuild_block(block: list[str], frags: list[str]) -> tuple[list[str], list[str], list[str]]:
    """拼一张表：行内续行并回上一行，表外残片归位或去重。"""
    notes: list[str] = []
    if not block:
        return list(block), list(frags), notes
    width = len(_cells(block[0]))
    if width < 2:
        return list(block), list(frags), notes

    items: list[dict[str, Any]] = []
    position = 0
    while position < len(block):
        raw = block[position]
        position += 1
        if _SEPARATOR.match(raw):
            items.append({"kind": "sep", "raw": raw, "cells": [], "merged": False})
            continue
        cells = _cells(raw)
        merged = False
        while position < len(block) and not _SEPARATOR.match(block[position]):
            following = _cells(block[position])
            evidence = _continuation_evidence(cells, following, width)
            if not evidence:
                break
            cell_text = cells[0] if cells else ""
            merged_note = ("把「%s | …」的续行拼回整行（表头 %d 列）：%s"
                           % (cell_text[:8], width, evidence))
            cells = _fit(cells + following, width)
            merged = True
            position += 1
            notes.append(merged_note)
        items.append({"kind": "row", "raw": raw, "cells": cells, "merged": merged})

    rows = [item for item in items if item["kind"] == "row"]
    leftover: list[str] = []
    if rows and frags:
        tail = rows[-1]
        consumed = 0
        if len(tail["cells"]) < width:
            frag_cells: list[str] = []
            while consumed < len(frags) and len(tail["cells"]) + len(frag_cells) < width:
                candidate = _cells(frags[consumed])
                current = tail["cells"] + frag_cells
                trimmed = _trim_repeated_head(candidate, current)
                if not trimmed or not _fragment_belongs_to(trimmed, current):
                    break
                frag_cells.extend(trimmed)
                consumed += 1
            if frag_cells:
                tail["cells"] = _fit(tail["cells"] + frag_cells, width)
                tail["merged"] = True
                notes.append("表外残片「%s」是表格行被甩出去的后半段，已拼回整行（表头 %d 列）"
                             % (frags[0].strip()[:24], width))
        indexes = {str(row["cells"][0]).strip(): row["cells"] for row in rows if row["cells"]}
        for fragment in frags[consumed:]:
            head = _cells(fragment)
            if head and head[0] and head[0] in indexes:
                row_cells = list(indexes[head[0]])
                if len(row_cells) < width and head != row_cells:
                    # 表内那一行自己也缺列：残片很可能是它的后半段，删掉就是丢一行数据。
                    # 只有「残片与表内那一行逐格相同」（纯重复）才允许删 —— v9 事故 B 就是
                    # 残片被当成重复行删掉、而表内那一行始终没补全，导出后整行消失。
                    notes.append("保留表外残片「%s」：表内序号 %s 那一行也缺列，"
                                 "残片可能是它的后半段（删掉就是丢行）"
                                 % (fragment.strip()[:24], head[0]))
                    leftover.append(fragment)
                    continue
                reason = _debris_rejection(fragment)
                if not reason:
                    notes.append("删除表外重复残片「%s」：它的序号与表内某行相同，"
                                 "且去掉竖线后不超过 %d 字、无句读指代词、无长中文短语，"
                                 "导出后会变成一行裸文本"
                                 % (fragment.strip()[:24], _DEBRIS_MAX_CHARS))
                    continue
                notes.append("保留表外残片「%s」：序号虽与表内某行相同，但它%s，"
                             "按残片删除会误删正文" % (fragment.strip()[:24], reason))
            leftover.append(fragment)

    rebuilt: list[str] = []
    for item in items:
        if item["kind"] == "sep" or not item.get("merged"):
            rebuilt.append(item["raw"])
        else:
            rebuilt.append(_render(item["cells"]))
    return rebuilt, leftover, notes


def _join_impl(text: str) -> tuple[str, list[str]]:
    body = str(text or "")
    if "|" not in body:
        return body, []
    lines = body.splitlines()
    notes: list[str] = []
    out: list[str] = []
    position = 0
    while position < len(lines):
        if not _is_pipe_row(lines[position]):
            out.append(lines[position])
            position += 1
            continue
        start = position
        while position < len(lines) and _is_pipe_row(lines[position]):
            position += 1
        block = lines[start:position]
        frags: list[str] = []
        probe = position
        while probe < len(lines) and _is_row_fragment(lines[probe]):
            frags.append(lines[probe])
            probe += 1
        rebuilt, leftover, block_notes = _rebuild_block(block, frags)
        out.extend(rebuilt)
        out.extend(leftover)
        notes.extend(block_notes)
        position = probe
    if not notes:
        return body, []
    return "\n".join(out), notes


def join_split_table_rows(text: str) -> tuple[str, list[str]]:
    """把被换行拆开/甩到表外的表格行拼回整行；返回 (新正文, 处理说明)。

    幂等：拼回后的行已凑满表头列数、残片已消失，第二次执行返回原文 + 空说明。
    不抛异常：内部任何异常都退化为 (原文, [])。
    """
    try:
        return _join_impl(text)
    except Exception:  # noqa: BLE001 - 修复函数绝不能把生成任务带崩
        return str(text or ""), []


# ═══════════════════ ② 段末半句补成完整句 ═══════════════════

# 这些字结尾读起来是完整的（数量、单位、序数、日期），口径与 tail_repair 一致
_ENUM_ENDINGS = set("了等项条根件套台组个名天月日次级元吨米秒分号型款批次年月期")
# 这些结尾一定是没写完
_DANGLING = set("，,、；;：:（(【[《〈—～-·和与及或按为的对由须应将在把被从向以于其该要是并未并")
_SIGNATURE = re.compile(r"(?:签字|签章|盖章|公章|年\s*月\s*日|日期[:：]).{0,24}$")
_NUMBERED_LINE = re.compile(
    r"^\s*(?:\d{1,2}(?:\.\d+)*\s*[、.．)）]|[（(]\s*\d{1,2}\s*[）)]|第[一二三四五六七八九十]{1,3}[条项])")
_BULLET_LINE = re.compile(r"^\s*[-*•·+]\s+")
_UNIT_TAIL = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:mm|cm|m|kW|kN|MPa|kg|t|h|t/h|V|Hz|%|年|天|个月|小时|分钟|人|台|套|组|根|个|只|件)"
    r"|[一二三四五六七八九十两]+(?:年|天|个月|小时))$")
_ACCEPT_VERB = re.compile(r"接受|服从|遵守|遵照|执行|配合")
_ACCEPT_OBJECT = re.compile(r"[、，,]?\s*(?:安全|质量)\s*$")
_UNDERGROUND = re.compile(r"矿方|井下|矿井|煤矿|采掘|工作面")
_MIN_PROSE = 20
_MIN_NUMBERED = 12

# ── A4 判据（v1 事故后收紧）：只在「整行末尾确实是悬空片段」时，才在**行尾**追加 ──
# a) 行末是这些分隔符 → 断在句中，不是段末（顿号/逗号上补句号会把并列项劈开）
_TRAILING_SEPARATORS = set("，,、；;：:")
# b) 这些字收尾读起来是完整的（「…的要求」「…的名称」「…的金额」），不能当断尾补
_CLOSING_CHARS = set("的了着者等名称日期金额元%")
# c) 行首中文编号（「一、」「（一）」）也算「完整句子结构」的证据
_CN_NUMBERED = re.compile(
    r"^\s*(?:[一二三四五六七八九十]{1,3}\s*[、.]|[（(]\s*[一二三四五六七八九十]{1,3}\s*[）)])")
# d) 下一行以这些字符起头 → 是同一句被硬折行后的续写，这一行的行尾不是句子末尾
_WRAP_START = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（(《「『【—]")
# 只检出、不修改：标准名 / 英文或数字编码 / 模板占位 / 照录文书正文
_STANDARD_NAME = re.compile(r"[《》]")
_CODE_TOKEN = re.compile(r"[A-Za-z]")
_TEMPLATE_TOKEN = re.compile(r"\{\{|\}\}")
_QUOTED_DOC = re.compile(
    r"委托书|授权书|兹委托|现委托|委托代理|委托人[:：]|受托人[:：]|被授权人|授权代表|法定代表人|介绍信")
# 「投标人名称：…」「日期：…」这类标签行：冒号左边是字段名，不是投标正文，补了也是垃圾
_LABEL_LINE = re.compile(r"^\s*[\u4e00-\u9fffA-Za-z（）()]{1,12}[:：]")


def _complete_tail(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return True
    if stripped[-1] in _TERMINAL or stripped.endswith("}}"):
        return True
    if _SIGNATURE.search(stripped):
        return True
    if _UNIT_TAIL.search(stripped):
        return True
    return stripped[-1] in _ENUM_ENDINGS


def _is_prose_line(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False
    if stripped.startswith(("|", "#", "```", "~~~", ">", "<")):
        return False
    if stripped.count("|") >= 2:
        return False
    # 编号小标题（「12.2.7 安装调试与验收配合」）本身不写句号，不是半句
    if _HEADING.match(stripped) and len(stripped) <= 60:
        return False
    if _PLACEHOLDER_CELL.match(stripped):
        return False
    if _BULLET_LINE.match(stripped):    # 项目符号行是标签式写法，不要求句号
        return False
    return True


def _truncation_reason(line: str) -> str:
    """这一行是不是「写到一半断了」；是就返回原因，不是返回空串。"""
    stripped = str(line or "").strip()
    if not _is_prose_line(stripped) or _complete_tail(stripped):
        return ""
    numbered = bool(_NUMBERED_LINE.match(stripped))
    if len(stripped) < (_MIN_NUMBERED if numbered else _MIN_PROSE):
        return ""
    last = stripped[-1]
    if last in _DANGLING:
        return "句末停在连接词/介词（%s）" % last
    if re.search(r"[A-Za-z/\\-]$", stripped):
        return "句末停在半个词/单位（%s）" % stripped[-6:]
    if re.search(r"[\u4e00-\u9fff]$", stripped):
        return "句末无标点，停在半句"
    return ""


def truncated_paragraphs(text: str) -> list[str]:
    """找出「段末半句」的原文行（检测口径，供日志/测试；不改文本）。"""
    return [line.strip() for line in str(text or "").splitlines() if _truncation_reason(line)]


def _complete_accept_object(sentence: str) -> str:
    """「……接受招标人及矿方的安全、质量」→ 补同一句的宾语后收句。"""
    if not _ACCEPT_VERB.search(sentence) or not _ACCEPT_OBJECT.search(sentence):
        return ""
    tail = "监督与检查"
    if _UNDERGROUND.search(sentence):
        tail += "，严格执行矿方井下作业规程"
    return sentence + tail + "。"


def _strip_dangling(sentence: str) -> str:
    stripped = sentence.rstrip()
    while stripped and stripped[-1] in _DANGLING:
        stripped = stripped[:-1].rstrip()
    return stripped


def _readonly_reason(line: str) -> str:
    """这一行「只检出、不修改」的证据；没有就返回空串（A4 第 4 条）。"""
    stripped = str(line or "").strip()
    if "|" in stripped or _SEPARATOR.match(stripped):
        return "表格行"
    if _STANDARD_NAME.search(stripped):
        return "含标准名《》"
    if _TEMPLATE_TOKEN.search(stripped):
        return "含 {{…}} 占位"
    if _CODE_TOKEN.search(stripped):
        return "含英文/数字编码"
    if _QUOTED_DOC.search(stripped):
        return "照录文书（委托书/授权书）正文"
    if _LABEL_LINE.match(stripped):
        return "紧邻冒号的标签行"
    return ""


def _has_sentence_anchor(line: str, dangling: bool) -> bool:
    """A4-c：行内必须有完整句子结构（已闭合的 `。` 或行首编号），否则行末必须是悬空连接词。

    折行残片（「…修改询比采购项目响」）两样都不满足：它既不是一句的开头，句中也还没有
    任何句号，凭什么认为它的行尾就是句子末尾。
    """
    if dangling:
        return True
    if "。" in line:
        return True
    return bool(_NUMBERED_LINE.match(line) or _CN_NUMBERED.match(line))


def _is_wrapped_continuation(following: str) -> bool:
    """A4-d：紧邻的下一行是同一句的续写（模型把一句硬折成两行）→ 这行的行尾不是句子末尾。

    只认结构性证据：下一行不以标点收句起头，也不是编号/标题/项目符号/表格/占位，
    却直接接着上一行写中文、字母、数字或左括号 —— 那就是一句被折开了。
    下一行是编号条款（「3. …」）或表格/标题、或者中间有空行（段落分界）时都不算续写，
    正常段落末尾照旧可以补。
    """
    nxt = str(following or "").strip()
    if not nxt:
        return False
    if nxt[0] in _TERMINAL:
        return False
    if nxt.startswith(("|", "#", "```", "~~~", ">")):
        return False
    if (_HEADING.match(nxt) or _NUMBERED_LINE.match(nxt) or _CN_NUMBERED.match(nxt)
            or _BULLET_LINE.match(nxt) or _PLACEHOLDER_CELL.match(nxt)):
        return False
    return bool(_WRAP_START.match(nxt))


def _complete_one(line: str, following: str = "") -> tuple[str, str]:
    """把一行半句补成完整句；返回 (补全结果, 依据)；补不出来返回 ("", "")。

    **只在行尾追加，绝不在行内插入**。动手前四道证据同时成立（见模块说明 A4）：
    a) 行末没有句末标点/顿号逗号/序号/单位/落款；b) 行末不是常见收尾字；
    c) 行内有完整句子结构或行末是悬空连接词；d) 下一行不是同一句的续写。
    含《》标准名、英文/数字编码、`{{…}}`、表格行、照录文书正文一律不动。

    补全依据只取「同一句里已经有的东西」（`接受…安全、质量` → 监督与检查；去掉悬空
    连接词后收句），**拿不出具体宾语就不改**：通用宾语「等方面的监督与管理」已经把
    「响应文件」「《煤矿设备检修技术规范》」「德国 SIMRIT 原装进口氟胶油封」劈开过
    5 次，不再保留这条兜底。
    """
    stripped = str(line or "").strip()
    reason = _truncation_reason(stripped)
    if not reason:
        return "", ""
    if _readonly_reason(stripped):
        return "", ""
    last = stripped[-1]
    if last in _CLOSING_CHARS:
        return "", ""
    # 注意：`、，；：` 同时属于「分隔符」和「悬空连接词」。旧写法在这里直接 return，
    # 于是「…施工方案如下、」这类顿号收尾的断句永远被跳过（A4 漏检）。
    # 只有「分隔符里不属于悬空连接词的那些」才算收尾标点。
    if last in _TRAILING_SEPARATORS and last not in _DANGLING:
        return "", ""
    dangling = last in _DANGLING
    if not _has_sentence_anchor(stripped, dangling):
        return "", ""
    if _is_wrapped_continuation(following):
        return "", ""
    completed = _complete_accept_object(stripped)
    if completed:
        return completed, reason + "；补同一句的自然宾语"
    if not dangling:
        # 名词性收尾：没有具体宾语就一个字都不加（旧版在这里塞通用宾语，酿成 v1 事故）
        return "", ""
    trimmed = _strip_dangling(stripped)
    if not trimmed:
        return "", ""
    if trimmed[-1] in _TERMINAL:
        return trimmed, reason + "；去掉悬空连接词后收句"
    if trimmed[-1] in _ENUM_ENDINGS or _UNIT_TAIL.search(trimmed):
        return trimmed + "。", reason + "；去掉悬空连接词后以量词/单位收尾，补句号"
    if re.search(r"[\u4e00-\u9fff]$", trimmed):
        return trimmed + "。", reason + "；去掉悬空连接词后收句（不补宾语）"
    return "", ""


def _complete_impl(text: str) -> tuple[str, list[str]]:
    body = str(text or "")
    if not body.strip():
        return body, []
    lines = body.splitlines()
    notes: list[str] = []
    rebuilt: list[str] = []
    for index, line in enumerate(lines):
        # 折行残片上**紧邻**的下一行才是同一句的续写；空行是段落分界，不算续写
        following = lines[index + 1] if index + 1 < len(lines) else ""
        completed, evidence = _complete_one(line, following)
        if not completed:
            rebuilt.append(line)
            continue
        rebuilt.append(completed)
        notes.append("补全段末半句「%s…」→「…%s」（依据：%s）"
                     % (line.strip()[-16:], completed[-16:], evidence))
    if not notes:
        return body, []
    return "\n".join(rebuilt), notes


def complete_truncated_paragraphs(text: str) -> tuple[str, list[str]]:
    """把段末半句补成完整句；返回 (新正文, 处理说明)。

    **只在行尾追加，绝不在行内插入**：每一行的新文本都必须以「该行原文（rstrip 后）」开头，
    否则整篇退回原文 —— 把补语拼到硬折行的行尾，导出后就是插在句子中间，把词劈开
    （v1 事故 5 处：`…项目响│等方面的监督与管理。│应文件`）。

    拿不出同一句里的具体宾语就**不改**（不再补「等方面的监督与管理」「等方面的内容」
    「等要求」这类通用兜底）：证据不足宁可交人工，也不制造新的半句。

    幂等：补全后的句子以「。」结尾，第二次执行返回原文 + 空说明。
    不抛异常：内部任何异常都退化为 (原文, [])。
    """
    original = str(text or "")
    try:
        completed, notes = _complete_impl(original)
    except Exception:  # noqa: BLE001 - 修复函数绝不能把生成任务带崩
        return original, []
    if not notes or completed == original:
        return original, []
    before = original.splitlines()
    after = completed.splitlines()
    if len(before) != len(after):
        return original, []
    appended = 0
    for old, new in zip(before, after):
        if new == old:
            continue
        # 行内插入 / 劈词 / 改行首一律不许发生。只接受两种形态：
        #   ① 原行末尾接着写（new 以 old 开头）；
        #   ② 先消掉行尾悬空连接词（、，；及和与或 等）再收句，即 new 以「消尾后的旧行」开头。
        old_core = old.rstrip().rstrip("、，,；;及和与或")
        if not (
            new.startswith(old.rstrip())
            or new.startswith(old.strip())
            or (old_core and new.startswith(old_core))
        ):
            return original, []
        # 长度基准必须用「消尾后的旧行」：悬空连接词收句是「删 1 字 + 补 1 个句号」，
        # 相对 old.strip() 是等长的，拿它当基准会把这种修补误判成「没追加」。
        if len(new) <= len(old_core if old_core and new.startswith(old_core) else old.strip()):
            return original, []
        appended += 1
    if appended != len(notes):
        return original, []
    return completed, notes
