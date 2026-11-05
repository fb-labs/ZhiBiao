"""报价防重复计价提示：同一张表里「永磁…滚筒」与「传动滚筒」并列时，表后补一行备注。

背景（线上问题）
----------------
10.7.2《单台整机供货明细》里同时有：

    | 2 | 永磁同步电动滚筒 | 2 | 台 |
    | 7 | 传动滚筒 φ1000mm | 2 | 件 |

这两个条目指的是**同一套驱动结构**：永磁同步电动滚筒本身就把传动滚筒（φ1000mm）
做进了一体结构（或作为配套部件随机供货）。评标/审计看到两行会怀疑重复计价，
填报人也常来问「这两行是不是要报两次价」。

做法
----
* **不动表格结构**（行、列、合计口径一律不改）：只在**该表后面紧跟一行**备注；
* 判定条件做成通用规则，不写死项目：同一张表里同时存在
  「既含永磁、又含滚筒」的行 与 「含传动滚筒」的行；
* **备注是固定句，只有一个可变部分**：传动滚筒的规格。规格只从**该传动滚筒单元格
  自己的括号**里取（`（φ1000mm）` / `(φ1000mm)` / `φ1000mm`），取不到就不写规格；
  永磁那一侧永远写固定词「永磁同步电动滚筒」；
* 规格提取带硬约束（`φ`/`Φ`/`直径` + 数字 + `mm`、≤12 字、不含 `；，。`），
  任一条不满足即判失败、退回不带规格的固定句——**绝不从其它单元格、整行、表头
  或相邻行拼接任何文字**（v7 实测的 5 处乱码就是拼接相邻单元格碎片造成的）；
* 同一份正文里已经有同类备注就不再重复加（幂等）；
* 备注是给人看的填报提示，**不参与合计计算**：它不在表格里，任何按表格行求和的
  逻辑都读不到它。
"""

from __future__ import annotations

import re

#: 永磁滚筒：一个单元格里既有「永磁」又有「滚筒」即算（永磁同步电动滚筒 / 永磁电动滚筒 /
#: 矿用隔爆型永磁同步电动滚筒 …），不绑定具体型号。**只用于「同表并列」判定，
#: 不进入备注文本**。
_MAGNET_DRUM_NAME = re.compile(r"[^|｜\s]{0,12}永磁[\u4e00-\u9fffA-Za-z0-9]{0,12}?滚筒")
#: 传动滚筒（铭牌/表里也常写成「传动滚筒（改向）」）。
_TRANSMISSION_DRUM = "传动滚筒"
#: 备注里永磁那一侧的固定词（不从表里取名，杜绝名称串味）。
_MAGNET_NOTE_NAME = "永磁同步电动滚筒"

#: 规格白名单：只认 φ/Φ/直径 + 数字（1~4 位）+ mm。
_SPEC_IN_TEXT = re.compile(r"(?:φ|Φ|直径)\s*\d{1,4}\s*mm", re.IGNORECASE)
#: 整段候选必须是「φ/Φ/直径 + 数字 + mm」，前后不允许夹带别的字。
_SPEC_STRICT = re.compile(
    r"^(?:φ|Φ|直径)\s*(?P<digits>\d{1,4})\s*mm$", re.IGNORECASE
)
#: 单元格里的括号内容（中英文括号都认；括号内不再嵌括号）。
_BRACKET_CONTENT = re.compile(r"[（(]([^（()）]*)[)）]")
#: 规格硬约束：长度上限与禁用字符，命中任一条即判失败（不用这段文字拼备注）。
_SPEC_MAX_LEN = 12
_SPEC_BAD_CHARS = ("；", "，", "。")

#: 表行：`| … |`（首尾竖线有无都认）。
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
#: 表格分隔行：| --- | :---: |
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

#: 备注键：既用于去重，也用于「这句话是提示，不是数据」的识别。
NOTE_KEY = "不重复计价"
NOTE_PREFIX = "注："
#: 关系描述前缀：备注列里的「与永磁滚筒配套」「含传动滚筒」不是条目名
#: （只按**开头**判，不能用子串判——「永磁同步电动滚筒」里也有「同」）。
_RELATION_PREFIXES = ("与", "含", "配", "跟", "及", "另", "同", "见")
#: 现成的同类备注（正文里已有就不再加）。
_EXISTING_NOTE = re.compile(r"^\s*注[:：].*" + NOTE_KEY, re.MULTILINE)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """正文里每张表的 [起, 止) 行号区间（连续的表行算一张表）。"""
    blocks: list[tuple[int, int]] = []
    start = None
    for index, line in enumerate(lines):
        if _TABLE_ROW.match(line):
            if start is None:
                start = index
        elif start is not None:
            blocks.append((start, index))
            start = None
    if start is not None:
        blocks.append((start, len(lines)))
    return blocks


def _table_rows(lines: list[str], block: tuple[int, int]) -> list[str]:
    """表里的数据行（去掉表头分隔行与空行）。"""
    start, end = block
    return [line for line in lines[start:end]
            if line.strip() and not _TABLE_SEP.match(line)]


def _magnet_drum_name(cell_text: str) -> str:
    """取「永磁…滚筒」的条目名（去掉序号与括号说明；取不到时返回空串）。

    仅供判定用：名称本身不进备注（备注里永磁那一侧是固定词）。
    """
    matched = _MAGNET_DRUM_NAME.search(cell_text)
    if not matched:
        return ""
    name = re.sub(r"^[\s|｜]*\d{1,3}[.、)）]?\s*", "", matched.group(0))
    name = re.sub(r"[（(][^)）]*[)）]", "", name).strip()
    return name


def _bracket_contents(cell_text: str) -> list[str]:
    """单元格里的括号内容（已 strip，可能为空串列表）。"""
    return [content.strip() for content in _BRACKET_CONTENT.findall(cell_text or "")]


def _format_spec(candidate: str) -> str:
    """候选规格 → 「φ1000mm」；不合格返回空串。

    硬约束（任一不满足即判失败，调用方退回不带规格的固定句）：
    * 只认 `φ`/`Φ`/`直径` + 数字（1~4 位）+ `mm`，整段不允许夹带别的文字；
    * 长度 ≤ 12 字；
    * 不含 `；`、`，`、`。`。
    """
    text = (candidate or "").strip()
    if not text or len(text) > _SPEC_MAX_LEN:
        return ""
    if any(bad in text for bad in _SPEC_BAD_CHARS):
        return ""
    matched = _SPEC_STRICT.match(text)
    if not matched:
        return ""
    return "φ%smm" % matched.group("digits")


def _drum_spec_from_cell(cell_text: str) -> tuple[str, bool]:
    """只从「传动滚筒」自己这个单元格里取规格，返回 (规格, 是否判失败)。

    * 先看括号内容：`传动滚筒（φ1000mm）` / `传动滚筒(φ1000mm)`；
    * 括号里没有规格意图就认单元格里的裸规格：`传动滚筒 φ1000mm`；
    * 候选一旦像规格却不满足硬约束（夹带 `；，。`、超过 12 字、不是
      `φ/Φ/直径 + 数字 + mm`），即判失败——**不再去别的单元格/整行拼规格**。
    """
    text = cell_text or ""
    for content in _bracket_contents(text):
        if _SPEC_IN_TEXT.search(content):
            spec = _format_spec(content)
            return spec, not spec
    bare = _SPEC_IN_TEXT.search(text)
    if bare:
        spec = _format_spec(bare.group(0))
        return spec, not spec
    return "", False


def note_text(drum_spec: str = "") -> str:
    """备注正文：固定句，只有「（规格）」随传动滚筒规格变化。"""
    spec = ("（%s）" % drum_spec) if drum_spec else ""
    return ("%s传动滚筒%s与%s为一体结构（或配套部件），其价格已含在%s内，%s。"
            % (NOTE_PREFIX, spec, _MAGNET_NOTE_NAME, _MAGNET_NOTE_NAME, NOTE_KEY))


def _table_hit(table_lines: list[str]) -> str | None:
    """这张表是否同时有「永磁…滚筒」行与「传动滚筒」行。

    命中返回传动滚筒规格（空串 = 不带规格），未命中返回 None。逐**单元格**判定，
    不拿整行拼起来判：「传动滚筒」那一行的备注列常写着「与永磁滚筒配套」，
    按整行判会把这一行误当成永磁滚筒行，于是永远判不出命中。

    规格只取第一处传动滚筒单元格**自己**的括号/裸规格：不跨单元格、不跨行拼接，
    所以相邻单元格的碎片（数量、单位、备注、上一行文字）一个字都进不来。
    """
    magnet_found = False
    transmission_found = False
    spec = ""
    for line in table_lines:
        for cell in _cells(line):
            name = _magnet_drum_name(cell)
            if name:
                # 「与永磁滚筒配套」这类关系描述不是条目名
                if not name.startswith(_RELATION_PREFIXES):
                    magnet_found = True
                # 与原逻辑一致：永磁单元格不再当作传动滚筒单元格取规格
                continue
            if _TRANSMISSION_DRUM in cell:
                if not transmission_found:
                    transmission_found = True
                    spec, _invalid = _drum_spec_from_cell(cell)
    if not (magnet_found and transmission_found):
        return None
    return spec


def add_drum_pricing_note(body: str) -> tuple[str, str]:
    """在命中的表格后面紧跟一行「不重复计价」备注；返回 (新正文, 处理说明)。

    表格结构一个字都不动；同一张正文里已有同类备注时原样返回（幂等）。
    """
    text = str(body or "")
    if not text or _EXISTING_NOTE.search(text):
        return body, ""
    lines = text.splitlines()
    spec: str | None = None
    insert_at = -1
    for block in _table_blocks(lines):
        spec = _table_hit(_table_rows(lines, block))
        if spec is not None:
            insert_at = block[1]
            break
    if spec is None or insert_at < 0:
        return body, ""
    note = note_text(spec)
    # 备注紧跟表尾，与前后文各空一行（是独立段落，不是表格行——合计口径读不到它）
    end = insert_at
    while end < len(lines) and not lines[end].strip():
        end += 1
    rebuilt = list(lines[:insert_at]) + ["", note]
    if end < len(lines):
        rebuilt += [""] + list(lines[end:])
    return "\n".join(rebuilt), "表后补「不重复计价」备注（传动滚筒%s）" % (
        spec or "未注规格")