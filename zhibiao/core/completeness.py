"""完整性核对：技术要求条目、边界条款、部件参数块、数量口径、表格结构。

线上事故（第二个标书v2，皮带机）：
* 4.1.3「关键部件与专项指标对照」表写到第 11 行「中间部」就断了：行里只有 5 个格子
  （表头 6 列），作用域列被写成「无偏离」、证明材料来源为空，后面托辊/机尾/机架联接/
  输送带四条技术要求再没有出现；
* 招标「二、技术要求」第 14 条（综合保护装置、通信控制系统由矿方自行配套）被反复响应，
  第 15 条（皮带变频开关由矿方自备）全文零次出现；
* 变频器参数（防爆标志 ExdIMb、400kW、1140V、50Hz、IP54、水冷、S1）只在个别小节出现，
  2.1.3 / 4.1.2 的参数对照表里根本没有变频器一行；
* 供货范围表把「变频器 2 台/单台整机」与别处「共 2 台」并列，两套口径互相矛盾。

本模块只做**判断**，不改正文：返回人类可读的中文问题描述，交给质检/一致性告警。
所有函数都是纯函数，且只在「原文确实有条目」时判定，避免换个行业就误报。
"""

from __future__ import annotations

import re
from typing import Any

# ─────────────────────────── 通用：Markdown 表格 ───────────────────────────

_TBL_LINE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TBL_SEP = re.compile(r"^[ \t]*\|?[ \t:|-]*-{3,}[ \t:|-]*\|?[ \t]*$")


def split_cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def md_tables(body: str) -> list[dict[str, Any]]:
    """抽出正文里所有 Markdown 表格：{header, rows, line}。"""
    lines = str(body or "").splitlines()
    tables: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        if not _TBL_LINE.match(lines[i].rstrip()):
            i += 1
            continue
        block: list[str] = []
        while i < len(lines) and _TBL_LINE.match(lines[i].rstrip()):
            block.append(lines[i].rstrip())
            i += 1
        if len(block) < 2:
            continue
        header = split_cells(block[0])
        rows = [split_cells(line) for line in block[1:] if not _TBL_SEP.match(line.strip())]
        if header and rows:
            tables.append({"header": header, "rows": rows, "line": block[0].strip()})
    return tables


def _col(header: list[str], *names: str) -> int:
    for index, cell in enumerate(header):
        text = str(cell or "")
        if any(name in text for name in names):
            return index
    return -1


# ─────────────────────────── 1. 表格结构完整性 ───────────────────────────

def table_structure_gaps(body: str) -> list[str]:
    """表格行单元格数与表头不一致 → 模型把行写残了（漏列/并列）。

    线上就是这一条：4.1.3 第 11 行只有 5 个格子，作用域列被塞了「无偏离」，
    证明材料来源整列丢失，导出后看起来像「列错位」，实际是行残缺。
    """
    gaps: list[str] = []
    for table in md_tables(body):
        width = len(table["header"])
        if width < 3:
            continue
        bad = []
        for row in table["rows"]:
            if len(row) != width:
                bad.append((len(row), row[0][:6] if row else ""))
        if bad:
            first = bad[0]
            gaps.append(
                f"表格「{table['header'][0]}…」有 {len(bad)} 行单元格数与表头（{width} 列）不一致"
                f"（如序号「{first[1]}」只有 {first[0]} 格），须按表头逐列补齐，不得并列或留空")
    return gaps


def scope_column_gaps(body: str) -> list[str]:
    """「作用域/证明材料来源」类列被写成结论或留空 → 对照表失真。

    实测：作用域列写「无偏离」、证明材料来源整列为空。作用域回答「这个值对哪台设备/哪一层
    适用」，证明材料回答「凭什么」，都不是「是否偏离」的结论列。
    """
    gaps: list[str] = []
    for table in md_tables(body):
        header = table["header"]
        scope_col = _col(header, "作用域", "适用对象", "适用范围", "适用层级")
        proof_col = _col(header, "证明材料", "证明文件", "依据来源", "证据")
        if scope_col < 0 and proof_col < 0:
            continue
        wrong_scope: list[str] = []
        empty_proof: list[str] = []
        for row in table["rows"]:
            if scope_col >= 0 and scope_col < len(row):
                cell = row[scope_col]
                if cell and re.fullmatch(r"(?:无偏离|完全响应|满足要求|响应|符合|无)", cell):
                    wrong_scope.append(cell)
            if proof_col >= 0 and proof_col < len(row) and not row[proof_col].strip():
                empty_proof.append(row[0][:6] if row else "")
        if wrong_scope:
            gaps.append(
                "「作用域/适用对象」列被写成了「" + "、".join(dict.fromkeys(wrong_scope))[:20]
                + "」这类结论：该列必须写适用对象与层级（如「单台整机」「单台永磁电动滚筒」），"
                  "是否偏离不在这一列表达")
        if empty_proof:
            gaps.append(
                "「证明材料来源」列有 " + str(len(empty_proof)) + " 行是空的（序号 "
                + "、".join(empty_proof[:6]) + "）：每行都要写清可核查的证明材料")
    return gaps


# ─────────────────────────── 2. 技术要求条目 ───────────────────────────

_TECH_START_WORDS = ("技术要求", "技术规格", "技术规范", "技术标准", "技术参数", "技术需求")
# 技术要求小节在「其他要求/供货范围/交货/包装…」之前结束。按标题**语义**判断，
# 不假定它是「二、」——换一份招标文件，技术要求可能是三、四、五，写死编号就会整段丢。
_TECH_END_WORDS = ("其他要求", "其它要求", "供货范围", "供货要求", "交货", "包装", "运输",
                   "售后服务", "技术服务", "质保", "质量保证", "验收", "培训", "备件",
                   "随机", "商务", "报价", "说明", "附件", "图纸")
_SECTION = re.compile(r"^[ \t]*([一二三四五六七八九十]{1,3})[ \t]*[、.．][ \t]*([^\n]{1,40})$")
# 兼容旧写法（「二、技术要求」行首带阿拉伯数字 2）
_TECH_HEAD_LEGACY = re.compile(r"^[ \t]*(?:二|２|2)[ \t]*[、.．][ \t]*技术要求")
# 条目号：行首最多 8 个空格的缩进 + 1~99 的阿拉伯数字 + 「、」/「.」/「．」。
# 缩进超过 8 个空格的按折行续行处理，不当条目号（PDF 折行常带缩进，带缩进的
# 「3.5m/s」这类参数行也不是条目）。
_ITEM = re.compile(r"^([ \t\u3000]{0,8})(\d{1,2})[ \t]*([、.．])[ \t]*(\S.*)$")
# 「像编号、其实是数值」的行：0.10mm-0.13mm。 / 1.5 倍 / 2.0 MPa
_DECIMAL_LIKE = re.compile(r"^[ \t\u3000]{0,8}\d{1,3}[ \t]*[.．][ \t]*\d")
# 「1.mm」这种「数字 + 点 + 单位」的写法同样是数值，不是条目号
_UNIT_HEAD = re.compile(r"^(?:mm|cm|dm|km|kg|mg|kW|MPa|kPa|Pa|Hz|kHz|V|A|rpm|%|℃|°)")
_MAX_ITEM_INDENT = 8
# 诊断里最多留几条「疑似数值行」的原文样本（计数不受此限）
_MAX_DECIMAL_SAMPLES = 20
_CHAPTER_LINE = re.compile(r"^[ \t]*第[一二三四五六七八九十百零〇\d]{1,3}章")
_BOUNDARY = re.compile(
    r"由[ \t]*(?:矿方|甲方|招标人|采购人|使用单位|业主)"
    r"|(?:矿方|甲方|招标人|采购人|使用单位|业主)[ \t]*(?:自行)?(?:配套|自备|提供|负责|承担)")
# 条目首部动词/限定词：切掉之后剩下的才是「条目名」
_LEAD_VERB = re.compile(
    r"^(?:该机|所有|全部|本机|本次|每|各)?(?:采用|为|是|应|须|需|配|由|对|在|按|具有|具备|设置|设置好)")


def _clean_head(text: str) -> str:
    s = str(text or "").strip()
    s = re.split(r"[：:]", s)[0]
    s = re.split(r"[。；;，,（(]", s)[0]
    s = _LEAD_VERB.sub("", s)
    s = re.sub(r"^(?:该机|所有|全部|本机|本次|每|各)", "", s)
    for verb in ("采用", "应", "须", "需", "配", "由", "包括", "含", "具有", "要符合", "必须"):
        s = s.split(verb)[0]
    s = s.strip(" 　、，。：:")
    return s[:12]


def _is_tech_heading(line: str) -> bool:
    stripped = str(line or "").strip(" 　\t")
    if _TECH_HEAD_LEGACY.match(stripped):
        return True
    found = _SECTION.match(stripped)
    return bool(found) and any(word in found.group(2) for word in _TECH_START_WORDS)


def _tech_section(tender_text: str) -> list[str]:
    """技术要求小节的正文行。

    起点按语义找（含「技术要求/技术规格/技术规范…」的小节标题，章号不限）；
    终点按语义找（其他要求/供货范围/交货/包装/售后…），找不到就停在下一章。
    小节标题后面紧跟编号条目的（如「（一）驱动部分」下面还有 1、2、3），
    视为小节内的分块，不提前截断。
    """
    lines = str(tender_text or "").splitlines()
    start = -1
    for index, line in enumerate(lines):
        if _is_tech_heading(line):
            start = index
            break
    if start < 0:
        return []
    out: list[str] = []
    for offset, line in enumerate(lines[start + 1:]):
        stripped = line.strip(" 　\t")
        if _CHAPTER_LINE.match(stripped):
            break
        found = _SECTION.match(stripped)
        if found:
            title = found.group(2)
            if any(word in title for word in _TECH_END_WORDS):
                break
            # 小节标题后面紧跟编号条目 → 是技术要求内部的分块，继续往后读
            lookahead = [x.strip() for x in lines[start + 2 + offset: start + 6 + offset] if x.strip()]
            if not any(_item_head(x) for x in lookahead[:3]):
                break
        out.append(line)
    # 截到第一条编号条目之前的前言单独保留（不算条目）
    return out


def _item_head(line: str) -> tuple[int, str] | None:
    """一行是不是「条目号 + 正文」。是则返回 (编号, 正文)，否则 None。

    排除两类假条目：
    * 编号必须是 1~99（0 不是条目号）；行首缩进 > 8 个空格的按折行续行处理；
    * 分隔符是「.」/「．」时，后面紧跟数字或单位的属于小数/数值，不是条目号——
      线上事故（新3.pdf 第 32 页）就是折行残句「0.10mm-0.13mm。」被当成「第 0 条」。
      「、」后面跟数字是正常条目（如「3、5 台…」），所以只对点号做这项判断。
    """
    match = _ITEM.match(str(line or ""))
    if not match:
        return None
    indent = match.group(1)
    width = sum(4 if char == "\t" else 1 for char in indent)
    if width > _MAX_ITEM_INDENT:
        return None
    try:
        number = int(match.group(2))
    except (TypeError, ValueError):
        return None
    if not 1 <= number <= 99:
        return None
    delimiter = match.group(3)
    text = match.group(4).strip()
    if delimiter in ".．" and (text[:1].isdigit() or _UNIT_HEAD.match(text)):
        return None
    return number, text


class TechItems(list):
    """`tech_items` 的返回：仍然是 list，另外挂上解析诊断（不静默丢条）。

    字段：
    * ``diagnostics``：每条 ``{"kind", "expected", "actual", "text"}``，
      kind 取 ``decimal``（疑似小数/数值行，未按条目计）/ ``jump``（编号跳号，已保留）/
      ``regress``（编号倒退或重号，已丢弃）/ ``too_few``（编号段落太少）；
    * ``dropped``：被丢弃的条目数（编号倒退/重号）；
    * ``ignored``：被判定为小数/数值、没按条目计的行数；
    * ``total``：小节里命中的编号段落总数（dropped + 保留数）；
    * ``failed``：是否整体判为解析失败（此时列表为空，原因在 ``note``）；
    * ``note``：给界面显示的一句话，如「已跳过 1 条异常编号」。
    """

    def __init__(self, items: Any = (), diagnostics: Any = None, dropped: int = 0,
                 ignored: int = 0, total: int = 0, failed: bool = False, note: str = "") -> None:
        super().__init__(items)
        self.diagnostics: list[dict[str, Any]] = list(diagnostics or [])
        self.dropped = int(dropped)
        self.ignored = int(ignored)
        self.total = int(total)
        self.failed = bool(failed)
        self.note = str(note or "")

    @property
    def anomalies(self) -> int:
        """异常条数（含跳号、疑似数值行与已丢弃条）。"""
        return len(self.diagnostics)


def _tech_items_note(dropped: int, decimal_count: int, diagnostics: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if dropped:
        first = next((d for d in diagnostics if d["kind"] == "regress"), None)
        detail = ""
        if first:
            detail = (f"，如期望第 {first['expected']} 条、实得第 {first['actual']} 条"
                      f"「{first['text']}」")
        parts.append(f"已跳过 {dropped} 条异常编号{detail}")
    jumps = sum(1 for d in diagnostics if d["kind"] == "jump")
    if jumps:
        first = next((d for d in diagnostics if d["kind"] == "jump"), None)
        parts.append(f"另有 {jumps} 处编号跳号已按原文保留"
                     + (f"（期望第 {first['expected']} 条、实得第 {first['actual']} 条）" if first else ""))
    if decimal_count:
        sample = next((d["text"] for d in diagnostics if d["kind"] == "decimal"), "")
        parts.append(f"{decimal_count} 行疑似小数/数值（如「{sample}」）未按条目计")
    return "；".join(parts)


def tech_items(tender_text: str) -> TechItems:
    """技术要求小节下按 1、2、3…编号的条目（含边界条款，用 boundary 标记）。

    线上事故（新3.pdf，48 页询比采购）：体检显示「技术要求条目 0 条」。
    小节找到了、条目也命中了 26 条，但第 32 页的折行残句「0.10mm-0.13mm。」
    被当成「第 0 条」，编号在 (12) → (0) 处断裂，于是一份 26 条的要求**全部**返回 []。

    现在分两层，谁也不连坐整份：
    1. 数值/小数不当条目号（``_item_head``），折行残句并回上一条正文；
    2. 编号不合规只影响那一条：倒退/重号的单条丢弃并记诊断，按下一个期望编号继续匹配；
       前跳（1,2,5,6 这类原文编号本身不连续）照样保留、只记诊断，免得把真要求丢掉；
       只有丢弃条数 > 候选总数 1/3（原文编号本身不成体系）才整体判失败，
       且原因写进 ``note``/``diagnostics``，不静默返回空。
    """
    section = _tech_section(tender_text)
    if not section:
        return TechItems(note="原文里没有找到「技术要求」小节")
    items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    previous: int | None = None
    total = 0
    dropped = 0
    decimal_count = 0
    decimal_samples = 0
    for line in section:
        head = _item_head(line)
        if head is None:
            if _DECIMAL_LIKE.match(str(line or "")):
                decimal_count += 1
                if decimal_samples < _MAX_DECIMAL_SAMPLES:
                    decimal_samples += 1
                    diagnostics.append({"kind": "decimal", "expected": None, "actual": None,
                                        "text": str(line).strip()[:40]})
            if current is not None and line.strip():
                current["lines"].append(line.strip())
                current["text"] += " " + line.strip()
            continue
        number, text = head
        total += 1
        if previous is None:
            pass
        elif number == previous + 1:
            pass
        elif number > previous + 1:
            # 跳号：1,2,5,6 这类原文编号本身不连续，条目仍是真的，保留并记诊断
            diagnostics.append({"kind": "jump", "expected": previous + 1,
                                "actual": number, "text": text[:40]})
        else:
            # 倒退/重号：不是这条编号列表里的条目（如小节里另起的一段参数行），丢这一条
            diagnostics.append({"kind": "regress", "expected": previous + 1,
                                "actual": number, "text": text[:40]})
            dropped += 1
            current = None
            continue
        previous = number
        current = {"index": number, "text": text, "head": _clean_head(text),
                   "boundary": bool(_BOUNDARY.search(text)), "lines": [text]}
        items.append(current)
    if total < 3:
        # 少于 3 条编号段落时，「一条编号」和「一行参数」分不开，不硬判
        diagnostics.append({"kind": "too_few", "expected": None, "actual": total,
                            "text": f"只识别到 {total} 条编号段落"})
        return TechItems(diagnostics=diagnostics, dropped=dropped, ignored=decimal_count,
                         total=total, failed=True,
                         note=f"技术要求小节里只识别到 {total} 条编号段落，不足以当条目")
    if dropped * 3 > total:
        first = next((d for d in diagnostics if d["kind"] == "regress"), None)
        sample = (f"（如期望第 {first['expected']} 条、实得第 {first['actual']} 条"
                  f"「{first['text']}」）") if first else ""
        return TechItems(
            diagnostics=diagnostics, dropped=dropped, ignored=decimal_count,
            total=total, failed=True,
            note=(f"技术要求小节里 {total} 条编号有 {dropped} 条与前后编号不连续{sample}，"
                  f"超过 1/3，原文编号本身不成体系，已整体判为解析失败；请人工核对编号后重试"))
    return TechItems(items, diagnostics=diagnostics, dropped=dropped, ignored=decimal_count,
                     total=total, note=_tech_items_note(dropped, decimal_count, diagnostics))


def tech_item_gaps(tender_text: str, body: str, title: str = "") -> list[str]:
    """部件/专项对照表的行数必须覆盖技术要求条目数。

    只在正文确实像是「逐条对照表」时判定（章节标题含对照/响应/指标，表头含对照/响应/
    指标/部件，且有 ≥5 行），免得把方案叙述章也拦下来。行数缺 1 行以上即报
    （合并行只允许 1 行的容差）。

    线上案例：4.1.3「关键部件与专项指标对照」写到「中间部」就断了，只列 11 行，
    技术要求 1–13 条里的托辊、机尾、机架联接、输送带四条整段缺失。
    """
    if not _ITEM_TABLE_CHAPTER.search(str(title or "")):
        return []
    items = [item for item in tech_items(tender_text) if not item["boundary"]]
    if len(items) < 5:
        return []
    gaps: list[str] = []
    for table in md_tables(body):
        header = " ".join(table["header"])
        if not re.search(r"对照|响应|指标|部件|专项", header):
            continue
        if len(table["rows"]) < 5:
            continue
        rows = len(table["rows"])
        if rows >= len(items) - 1:
            continue
        hints = [f"{item['index']} {item['head']}" for item in items[max(0, rows - 2):]]
        gaps.append(
            f"对照表「{header[:12]}…」只列了 {rows} 行，招标「技术要求」共 {len(items)} 条"
            f"（缺 {len(items) - rows} 条）：逐条对照不得漏项，建议从第 {max(1, rows - 1)} 条起补齐（"
            + "、".join(hints[:6]) + "）")
    return gaps


# ─────────────────────────── 3. 边界条款（由矿方…） ───────────────────────────

_BOUNDARY_CHAPTER = re.compile(r"范围|边界|界面|供货")
# 「逐条对照」章：标题里必须同时体现参数/指标与对照/表，否则「服务响应时限承诺」
# 这类章节也会被要求列出全部技术条目（实测 seq 46 因此误报）。
_ITEM_TABLE_CHAPTER = re.compile(r"(?:参数|指标).*(?:对照|表)|(?:对照).*(?:参数|指标)")
# 公开别名：检查引擎（check_verify）必须用**同一个**判据认「逐条对照表」，
# 否则生成侧只检查「参数…对照」章，检查侧却把「服务响应时限表」也算进来，
# 于是同一份正文在生成门禁里合格、在自己的检查里被判「漏项必须修改」。
ITEM_TABLE_CHAPTER = _ITEM_TABLE_CHAPTER

def boundary_clauses(tender_text: str) -> list[dict[str, Any]]:
    """招标原文里「××由矿方自行配套 / 由矿方自备」的供货边界条款。"""
    out: list[dict[str, Any]] = []
    for item in tech_items(tender_text):
        if not item["boundary"]:
            continue
        text = item["text"]
        subject = re.split(r"由[ \t]*(?:矿方|甲方|招标人|采购人|使用单位|业主)", text)[0]
        names = [re.sub(r"^[、，,\s]+|[、，,\s]+$", "", part)
                 for part in re.split(r"[、，,和及]", subject)]
        names = [name for name in names if len(name) >= 2 and name not in ("该机", "所有", "全部")]
        qualifier = "自行配套" if "自行配套" in text else ("自备" if "自备" in text else "由矿方提供")
        out.append({"index": item["index"], "text": text, "items": names, "qualifier": qualifier})
    return out


def boundary_gaps(tender_text: str, body: str, title: str = "") -> list[str]:
    """每条边界条款都必须在本章出现，且写明「由矿方/不在我方供货范围」。

    只对「范围/边界/交付/供货/偏差/参数/指标」这类章节判定：技术要求章也讲边界，
    但「质量保证措施」章没有义务复述供货界面。
    """
    clauses = boundary_clauses(tender_text)
    if not clauses:
        return []
    if not _BOUNDARY_CHAPTER.search(str(title or "")):
        return []
    flat = re.sub(r"\s+", "", str(body or ""))
    gaps: list[str] = []
    for clause in clauses:
        missing = [name for name in clause["items"] if name not in flat]
        if missing:
            gaps.append(
                f"边界条款未响应：{clause['text'][:40]}"
                f"（缺 {'、'.join(missing[:4])}）；须写明该部分{clause['qualifier']}、不在我方供货范围")
    return gaps


# ─────────────────────────── 4. 部件参数块（变频器一类） ───────────────────────────

_LOOSE_PARAM = re.compile(r"^[ \t]*([\u4e00-\u9fa5A-Za-z/]{2,14})[ \t]*[：:][ \t]*(\S.*)$")


def component_blocks(tender_text: str) -> list[dict[str, Any]]:
    """技术要求里「4、变频器(厂家提供)：」这类以部件名开头、随后逐行「键：值」的块。"""
    out: list[dict[str, Any]] = []
    for item in tech_items(tender_text):
        if item["boundary"]:
            continue
        name = item["head"]
        params: dict[str, str] = {}
        for line in item["lines"]:
            match = _LOOSE_PARAM.match(line)
            if not match:
                continue
            key = match.group(1).strip()
            if any(word in key for word in ("由", "其中", "该", "本")):
                continue
            params[key] = match.group(2).strip()[:40]
        if len(params) >= 4:
            out.append({"index": item["index"], "name": name, "params": params})
    return out


# 章节类型决定「缺整个部件」要不要报：
# 只认真正的参数对照/指标对照章（标题同时出现 参数/指标 + 对照/表）。
# 实测把口径放成「驱动|配置」时，组织架构、知识转移、故障处理章都因为顺带提到
# 「变频器」三个字被判「参数不全」——这属于过度要求。
_PARAM_CHAPTER = re.compile(r"(?:参数|指标).*(?:对照|表)|(?:对照).*(?:参数|指标)")
_COMPONENT_NOUN = re.compile(r"(?:器|装置|滚筒|电机|电动机|减速器|阀|泵|托辊|机架|输送带|皮带|开关|控制箱)$")
# 条目名（驱动方式/储带及张紧）不是部件参数块，不能要求「补该部件」
_NOT_COMPONENT = re.compile(r"(?:方式|要求|结构|设计|参数|指标|措施|方案|标准|说明)$")


def component_gaps(tender_text: str, body: str, title: str = "") -> list[str]:
    """参数/指标对照章必须把部件参数块写进去（名称 + 至少 3 个参数值）。

    线上案例：变频器在 2.1.3 / 4.1.2 两张参数对照表里整行缺失，6.2.2 只写了一句
    「隔爆兼本安型」——招标原文的 ExdIMb、400kW、1140V、50Hz、IP54、水冷、S1 全丢。
    """
    blocks = component_blocks(tender_text)
    if not blocks:
        return []
    flat = re.sub(r"\s+", "", str(body or ""))
    heading = str(title or "")
    strict = bool(_PARAM_CHAPTER.search(heading))
    gaps: list[str] = []
    for block in blocks:
        name = block["name"]
        if not name or len(name) < 2 or _NOT_COMPONENT.search(name):
            continue
        if name not in flat:
            if strict and _COMPONENT_NOUN.search(name):
                gaps.append(
                    f"缺少部件「{name}」的响应：招标要求 {'、'.join(list(block['params'])[:5])}，"
                    f"本章须按原文列出该部件的参数")
            continue
        if not strict:
            continue
        present = sum(1 for key, value in block["params"].items()
                      if key in flat or (value and value in flat))
        if present < 3:
            gaps.append(
                f"部件「{name}」参数不全（只写到 {present} 项）：须照原文写全 "
                + "、".join(f"{key}{value}" for key, value in list(block["params"].items())[:6]))
    return gaps


# ─────────────────────────── 5. 数量口径 ───────────────────────────

_PER_MACHINE = re.compile(r"单台|每台|单机")
_TOTAL_QTY = re.compile(r"共[ \t]*(\d{1,3})[ \t]*(台|套|组|件|个)")
_QTY_NUM = re.compile(r"^\d{1,3}$")


def quantity_gaps(body: str) -> list[str]:
    """同一行里「N 台/单台整机」与「共 M 台」不能自相矛盾。

    实测：供货范围表写「变频器 2 台/单台整机（单台整机配 2 台，共 2 台）」——
    单台配 2 台、整机 2 套，总量应是 4 台，与「共 2 台」直接冲突，评标时就是前后不一致。
    """
    gaps: list[str] = []
    for table in md_tables(body):
        header = table["header"]
        qty_col = _col(header, "数量")
        unit_col = _col(header, "单位")
        name_col = _col(header, "部件", "名称", "供货对象", "项目", "指标")
        if qty_col < 0:
            continue
        for row in table["rows"]:
            if qty_col >= len(row):
                continue
            raw = row[qty_col]
            if not _QTY_NUM.match(raw):
                continue
            per_machine = unit_col >= 0 and unit_col < len(row) and bool(_PER_MACHINE.search(row[unit_col]))
            joined = " ".join(row)
            total = _TOTAL_QTY.search(joined)
            if not per_machine or not total:
                continue
            total_num = int(total.group(1))
            if total_num < int(raw) or (total_num == int(raw) and "整机" in joined):
                name = row[name_col][:20] if 0 <= name_col < len(row) else "该部件"
                gaps.append(
                    f"数量口径自相矛盾：「{name}」写 {raw} 台/单台整机，同一行又说「共 {total_num} 台」；"
                    "单台配置数量与项目总量必须分别写清，且与分项报价表一致")
    return gaps


def machine_count(tender_text: str) -> int | None:
    """招标原文里的整机/设备台数（用于「单台 × 台数 = 总量」的口径核对）。"""
    text = re.sub(r"\s+", "", str(tender_text or ""))
    patterns = (
        r"标段[：:][^，。]{0,30}?(\d{1,3})台",
        r"整机[^，。]{0,12}?(\d{1,3})台",
        r"(?:采购|招标)[^，。]{0,16}?(?:设备|整机|皮带机|支架)[^，。]{0,8}?(\d{1,3})台",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if 1 <= value <= 500:
                return value
    return None


# ─────────────────── 6. 原文存在性证据（体检「按证据扣分」用） ───────────────────
# 体检分要按证据扣分：只有**原文确实给了**的东西抽不到，才算抽取失败；原文本来没有的，
# 只能显示「原文未提供」，不许扣分。判断「原文有没有」必须与抽取器**解耦**：
# 抽取器自己的返回值不能同时用来证明「原文没有」（那样永远 0 分也永远说得通）。
# 所以下面这一层只扫原文形状：小节标题行、编号条目行、「由X方自备」句、成组「名：值」行、
# 「共 N 台/套」这类数量口径。纯函数，不落库、不调模型、不影响任何既有抽取结果。

# 数量口径字样：只认「整机/成套」的总量写法，不认「1 台减速器」这种部件数量。
# 与 `machine_count()` 的取值模式互为表里：这里回答「原文有没有写数量」，
# 那边回答「写了的能不能取到」。两边都不命中时，属「原文未提供」而非抽取失败。
_COUNT_IN_SOURCE = re.compile(
    r"共[ \t]*\d{1,3}[ \t]*(?:台|套|组)"
    r"|整机[^，。\n]{0,12}?\d{1,3}[ \t]*(?:台|套)"
    r"|\d{1,3}[ \t]*(?:台|套)[ \t]*(?:整机|成套)"
    r"|(?:采购|招标)[^，。\n]{0,16}?(?:设备|整机|货物|支架|输送机|滚筒)[^，。\n]{0,8}?\d{1,3}[ \t]*台"
    r"|标段[^，。\n]{0,20}?\d{1,3}[ \t]*台"
    r"|(?:数量|台数|总数量)[ \t]*[：:][^，。\n]{0,20}?\d{1,3}[ \t]*(?:台|套)")
# 参数块判据（一条条目内成组出现 ≥4 行「名：值」）与 `component_blocks()` 同口径。
_PARAM_BLOCK_MIN = 4


def _line_with(pattern: "re.Pattern[str]", text: str) -> str:
    for raw in str(text or "").splitlines():
        line = raw.strip(" 　\t")
        if line and pattern.search(line):
            return line
    return ""


def source_evidence(tender_text: str) -> dict[str, Any]:
    """原文「到底给了什么」的存在证据（体检判 extractable / not_in_source 的唯一依据）。

    返回：

    * ``tech_heading`` —— 命中的技术要求小节标题行（如「二、技术要求」）；
    * ``numbered_items`` / ``numbered_sample`` —— 技术小节里编号条目的行数与原文样本；
    * ``param_blocks`` / ``param_sample`` —— 带 ≥4 行「名：值」的部件参数块数量与样本；
    * ``boundary_line`` —— 「由X方自备/自行配套」的原文行（供货边界）；
    * ``machine_count_line`` —— 「共 N 台/套」「整机 N 台」这类数量口径的原文行。

    全部字段在原文没写时为空/0：调用方据此判「原文未提供」，**不拿抽取器的空结果
    反推原文没有**。条目/参数块的扫描与 `tech_items()` 共用 `_tech_section` / `_item_head`
    两个纯扫描函数，但不经过任何抽取结果，因此抽取器被改坏时这里仍然说真话。
    """
    source = str(tender_text or "")
    heading = next((line.strip(" 　\t") for line in source.splitlines()
                    if _is_tech_heading(line)), "")
    section = _tech_section(source)
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in section:
        if _item_head(line) is not None:
            current = {"text": str(line).strip(" 　\t"), "params": 0}
            entries.append(current)
            continue
        stripped = str(line or "").strip(" 　\t")
        if current is None or not stripped:
            continue
        if _LOOSE_PARAM.match(stripped):
            current["params"] += 1
    blocks = [entry for entry in entries if entry["params"] >= _PARAM_BLOCK_MIN]
    boundary_line = next((line.strip(" 　\t") for line in section
                          if _BOUNDARY.search(str(line or ""))), "")
    return {
        "tech_heading": heading[:60],
        "numbered_items": len(entries),
        "numbered_sample": [entry["text"][:40] for entry in entries[:3]],
        "param_blocks": len(blocks),
        "param_sample": [entry["text"][:40] for entry in blocks[:2]],
        "boundary_line": boundary_line[:60],
        "machine_count_line": _line_with(_COUNT_IN_SOURCE, source)[:60],
    }