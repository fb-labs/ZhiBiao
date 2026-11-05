"""按招标文件「投标文件格式」章出稿：文书照抄原文，表格按原列重建成可填表格。

为什么必须这么做：

形式评审的判据就是「投标文件格式是否符合招标文件第六章的规定」，一项不符即否决投标。
让模型「照着格式写」的结果是：投标函被写成 1.1~1.4 的长篇说明、偏差表被写成响应表、
报价表被拆成运费/培训费若干项 —— 内容也许更丰富，但形式评审一眼就不是招标人的表。

口径：
1. 招标文件给了该文书的格式原文 → 照抄，不增删词句（模板文字本身就是评分/评审依据）；
2. 原文里的表格被 PDF 拉平成「序号 名称 型号和规格 …」一行 → 还原成真表格，
   单元格留 {{待填:…}}，不编造价格/数量/型号；
3. 表格里的已知事实（设备名称、数量）可以从台账预填，其余一律待填；
4. 招标文件只列名称、没有格式原文 → 返回 None，由调用方退回通用文书或模型写作。

PDF 抽出来的表经常是「断」的：表头折到第二行、空行插在表头和数据之间、注释行被
排到表格上方、数据行「1 运输费」掉在表格外面、资格表退化成一行行纯文字。第五条
到第十条的口径就是把这些断点接回去 —— 表头能续、数据行能进表、注释和落款只能
排在表格下方。
"""
from __future__ import annotations

import re
from typing import Any

from zhibiao.core.text_hygiene import is_ellipsis_line
from zhibiao.core.bid_integrity import requires_authored_content

# 表头识别：这些词出现在同一行且被分隔成 4 段以上，基本就是表头
_HEADER_HINTS = (
    "序号", "名称", "型号", "规格", "数量", "单价", "总价", "交货期", "备注", "单位",
    "金额", "条款号", "偏差", "项目", "内容", "标准", "产地", "制造商", "重量",
    "品牌", "厂家", "税率", "合计", "小计", "技术参数", "响应", "偏离",
)
# 招标要求签字/盖章的落款行：照抄，且保留「（盖单位章）」这类提示文字
_SIGN_LINE = re.compile(r"(?:投标人|法定代表人|委托代理人|供应商|日期|年\s*月\s*日"
                        r"|单位公章|盖单位章|签字|签章)")
# 正文里被 PDF 拉平的表格行：至少 4 段、每段不长
_COL_SPLIT = re.compile(r"[ \t　]{2,}|\s*\|\s*")

_MIN_BODY = 60
# 必须由投标人自己写的章节：招标文件即便给了空白表格，也不能「照录空表」交差。
# 这类标题一律不走照录通道，交给模型按项目实际内容写。

# ── 表格还原用的识别规则 ───────────────────────────────────────────────
# 占位符行是数据行：它一旦出现就说明表头已经结束，后面只能是数据行
_PLACEHOLDER = "{{"
# 注释行：以「注」开头，只能排在表格下方（PDF 常把表下注释抽到表上面）。
# 不能只写 ^注：「注册地址」「注册资本」这类列名也是「注」字开头。
_NOTE_LINE = re.compile(r"^注(?:[：:\s]|[一二三四五六七八九十\d]|$)")
# 「说明：…」与「注：…」同类：只能排在表格下方，不能当列名并进表头。
# 「说明书」这种列名不算（要求后面是冒号/空白/顿号/行尾）。
_EXPLAIN_LINE = re.compile(r"^说\s*明(?:[：:\s、]|$)")
# 编号注释行：「2、本次报价为不含税价」。PDF 常把这类表下注释抽到表头上面。
# 只认「数字、」：表内数据行是「1  运输费」这种空白分列，不会命中。
_NUMBERED_NOTE = re.compile(r"^\d{1,3}\s*[、]")
_DATE_LINE = re.compile(r"^年\s*月\s*日$")
# 落款标签：整段就是签字/盖章栏，不是表头字段（「投标人名称：」不算落款）
# 后缀「签字/盖章」也算：「法定代表人签字：」就是落款栏，不能留在表格上方。
_SIGN_LABEL = re.compile(r"^(?:投\s*标\s*人|供\s*应\s*商|法定代表人|委托代理人|授权代表人?"
                         r"|授权代理人|单位负责人|日期|年月日|签字|签章|盖章|单位公章)"
                         r"(?:签字|签章|盖章|公章)?$")
_BRACKET = re.compile(r"[（(][^）)]*[）)]")
# 落款后面只剩「（盖单位章）」「________」这类待签内容
_SIGN_VALUE_JUNK = re.compile(r"^(?:(?:[（(][^）)]{0,12}[）)])|[_＿—－\-\s　])*$")
_PAIR = re.compile(r"^(?P<label>[^：:]{1,12})[：:]\s*(?P<value>.*)$")
# 折行表头里不能出现的句读（括号不算：「单价（元）」是列名）
_CONT_PUNCT = "。；，、！？：:;,.!?"
# 折行续行的结构判定（不依赖「序号/数量」这类提示词）：
# 每段列名 ≤6 字（括号里的计量单位不算字，「出厂价总价（元/台）」是 5 字）、每段 ≥2 字；
_CONT_MAX_CHARS = 6
_CONT_MIN_CHARS = 2
# 续行片段数的上限 = 表宽上限 - 已认列数：一张报价/清单表最多按这么多列还原，
# 超了就说明这行不是「补齐剩余列」，而是表下的说明文字
_MAX_TABLE_COLUMNS = 12
# 数据行常常比表头窄（实测 6 列表头下面是 5 格数据行），剩余列名额至少留这么多
_CONT_MIN_ROOM = 3
# 资格表列名常见的结尾词：短词行里有两个以上命中，基本就是一行列名
_FIELD_SUFFIX = ("名称", "地址", "代码", "资本", "电话", "传真", "编号", "日期", "单位",
                 "金额", "产地", "证号", "账号", "开户", "银行", "类型", "性质", "范围",
                 "期限", "姓名", "职务", "邮箱", "邮编", "网址", "总额", "数量", "规格",
                 "型号", "备注", "内容", "项目", "级别", "类别", "等级", "规模", "方式",
                 "状态", "地点", "年限", "资质", "人", "年")
_ROW_LEAD = re.compile(r"^\d{1,3}[.．、)）]?$")


def _hint_count(tokens: list[str]) -> int:
    """有多少列名命中表头词库（按包含匹配，「招标文件章节及条款号」也算条款号列）。"""
    return sum(1 for token in tokens if any(hint in token for hint in _HEADER_HINTS))


def split_columns(line: str) -> list[str]:
    """把「序号 名 称 型号和规格 数量 …」这种被拉平的一行拆成列名。

    带 {{待填:…}} 的行是数据行，不能当表头：否则会套出 {{待填:{{待填:…}}}} 这种
    坏占位符（PDF 里占位行紧跟表头时实测会这么崩）。
    """
    text = str(line or "").strip()
    if not text or _PLACEHOLDER in text:
        return []
    parts = [p.strip() for p in _COL_SPLIT.split(text) if p.strip()]
    if len(parts) < 4:
        tokens = text.split()
        if len(tokens) >= 4:
            parts = tokens
    if len(parts) < 4 or any(len(p) > 12 for p in parts):
        return []
    # 「名 称」这种被 PDF 拆成两个字的列名先合并，再判是不是表头
    merged: list[str] = []
    index = 0
    while index < len(parts):
        if index + 1 < len(parts) and len(parts[index]) == 1 and len(parts[index + 1]) == 1:
            merged.append(parts[index] + parts[index + 1])
            index += 2
            continue
        merged.append(parts[index])
        index += 1
    if len(merged) < 4 or any(len(p) > 12 for p in merged):
        return []
    return merged if _hint_count(merged) >= 3 else []


def _simple_tokens(line: str) -> list[str]:
    """按 PDF 的列间距切词；只有单空格时退回按空白切。"""
    text = str(line or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _COL_SPLIT.split(text) if p.strip()]
    if len(parts) < 2:
        parts = text.split()
    return parts


def _is_sign_line(line: str) -> bool:
    """落款行：标签是签字/盖章栏，后面只剩「（盖单位章）」或空白。"""
    text = str(line or "").strip()
    if not text:
        return False
    if _DATE_LINE.match(text):
        return True
    match = _PAIR.match(text)
    label = _BRACKET.sub("", (match.group("label") if match else text).strip())
    value = (match.group("value") if match else "").strip()
    if not _SIGN_LABEL.match(re.sub(r"\s+", "", label)):
        return False
    return bool(_SIGN_VALUE_JUNK.match(value))


def _is_tail_line(line: str) -> bool:
    """注释行与落款行：照抄，但必须排在表格下方。"""
    text = str(line or "").strip()
    if not text:
        return False
    return bool(_NOTE_LINE.match(text)) or _is_sign_line(text)


def _tail_rank(line: str) -> int:
    """表格下方附注的类别：0=注释行、1=落款行、-1=不是表下附注。

    注释行包含「注：…」和「2、…」这类编号注释；两者都必须排在表格之后，
    且注释在前、落款在后。
    """
    text = str(line or "").strip()
    if not text:
        return -1
    if _NOTE_LINE.match(text) or _NUMBERED_NOTE.match(text):
        return 0
    return 1 if _is_sign_line(text) else -1


def _column_key_len(token: str) -> int:
    """列名有几个字：括号里的计量单位不算（「出厂价总价（元/台）」= 5 字）。

    PDF 常把单位塞进列名，按原始长度判会把「出厂价总价（元/台）」这种正常列名判成一句话。
    """
    text = str(token or "").strip()
    key = _BRACKET.sub("", text).strip()
    return len(key) if key else len(text)


def _short_columns(line: str) -> list[str]:
    """整行只是一串短列名：2~6 段、每段 ≤8 字（括号里的单位不算字）、没有句读、没有数字。"""
    text = str(line or "").strip()
    if not text or _PLACEHOLDER in text:
        return []
    if _is_tail_line(text) or _DATE_LINE.match(text):
        return []
    if text[0].isdigit():
        return []
    if any(ch in text for ch in _CONT_PUNCT):
        return []
    tokens = _simple_tokens(text)
    if not 2 <= len(tokens) <= 6:
        return []
    if any(_column_key_len(token) > 8 for token in tokens):
        return []
    if any(re.fullmatch(r"[\d.．%％]+", token) for token in tokens):
        return []
    return tokens


def _continuation_columns(line: str) -> list[str]:
    """表头折行：整行只有几个短列名、没有句读 → 并回同一表头，不能甩到表外。

    这是**结构判断**，不看「序号/数量」这类提示词：「单价（元） 出厂价总价（元） 原产地」
    一个提示词都没有，但它就是折到第二行的列名，照样要并回表头。
    是否真的并回由 `_header_continuation` 结合上下文（剩余列数、下一行是不是数据行）决定。
    """
    tokens = _short_columns(line)
    if not tokens:
        return []
    if any(not _CONT_MIN_CHARS <= _column_key_len(token) <= _CONT_MAX_CHARS
           for token in tokens):
        return []
    return tokens


def _remaining_columns(headers: list[str], follower: str) -> int:
    """表头还能补几列：表宽上限减去已认列数，按下一行数据行的列数收敛。

    PDF 抽出的数据行常常比表头窄（6 列表头下面是 5 格数据行），所以数据行列数只能当参考，
    至少留 `_CONT_MIN_ROOM` 列余量；上限由 `_MAX_TABLE_COLUMNS` 兜住。
    """
    width = len(_simple_tokens(follower)) if follower else 0
    room = max(width - len(headers), _CONT_MIN_ROOM)
    return min(room, _MAX_TABLE_COLUMNS - len(headers))


def _header_continuation(line: str, headers: list[str], follower: str) -> list[str]:
    """折行表头续行：结构判断 + 「正好补齐剩余列」+ 「下一行必须是数据行」。

    三条同时成立才并入表头，否则保持原样 —— 表下的说明文字（「说明：以上价格均为不含税价。」）
    段落短、没有句读时也不能被吞进表头。
    """
    tokens = _continuation_columns(line)
    if not tokens:
        return []
    if len(tokens) > _remaining_columns(headers, follower):
        return []
    if not _is_data_row(follower, len(headers) + len(tokens)):
        return []
    return tokens


def _names_only_columns(line: str) -> list[str]:
    """「投标人名称 注册地址 法定代表人 …」：只有列名、没有值的一行（资格表表头）。"""
    text = str(line or "").strip()
    if not text or _PLACEHOLDER in text:
        return []
    if _is_tail_line(text) or _DATE_LINE.match(text) or text[0].isdigit():
        return []
    if any(ch in text for ch in "。；，、！？：:;"):
        return []
    tokens = _simple_tokens(text)
    if len(tokens) < 3 or any(len(token) > 12 for token in tokens):
        return []
    field_like = sum(1 for token in tokens if token.endswith(_FIELD_SUFFIX))
    return tokens if field_like >= 2 else []


def _pair_rows(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """「列名：值」/「列名 + 空白待填格」的竖排行组；空白格保留成空格。"""
    rows: list[list[str]] = []
    index = start
    while index < len(lines):
        text = str(lines[index]).strip()
        if _is_tail_line(text):
            break
        match = _PAIR.match(text)
        if not match:
            break
        label = match.group("label").strip()
        value = match.group("value").strip()
        if not label or len(label) > 12 or any(ch in label for ch in "。；，、！？;,"):
            break
        if len(value) > 30 or any(ch in value for ch in "。；;,"):
            break
        rows.append([label, value or " "])
        index += 1
    return rows, index


def _is_data_row(line: str, columns: int) -> bool:
    """数据行：占位符行，或「数字 + 名称」开头的行（1 运输费 / 2 运输保险费）。"""
    text = str(line or "").strip()
    if not text:
        return False
    if _PLACEHOLDER in text:
        return True
    if _is_tail_line(text) or "。" in text:
        return False
    cells = _simple_tokens(text)
    if not cells or not _ROW_LEAD.match(cells[0]):
        return False
    if len(cells) > columns + 2 or any(len(cell) > 40 for cell in cells):
        return False
    return True


def _looks_like_row(line: str, columns: int) -> bool:
    """表头已经结束（出现过占位行）之后，短行按数据行处理，不能把表格切断。"""
    text = str(line or "").strip()
    if not text or _is_tail_line(text):
        return False
    if text[0] in "（(第":
        return False
    cells = _simple_tokens(text)
    if not 2 <= len(cells) <= columns + 2:
        return False
    if any(len(cell) > 40 for cell in cells):
        return False
    return not any(ch in text for ch in "。；：")


def _is_total_row(line: str, columns: int) -> bool:
    """「合计（列入"投标函"）」行：本应是表格最后一行。

    PDF 常把这一行排成表格外面的段落（有的还带「：」），于是表格少了合计行。
    """
    text = str(line or "").strip()
    if not text or _PLACEHOLDER in text or "。" in text:
        return False
    if not re.match(r"^合\s*计", text):
        return False
    cells = _simple_tokens(text)
    if not 1 <= len(cells) <= max(columns, 1) + 4:
        return False
    return all(len(cell) <= 40 for cell in cells)


def markdown_table(headers: list[str], rows: list[list[str]] | None = None) -> str:
    head = "| " + " | ".join(headers) + " |"
    rule = "|" + "|".join(["---"] * len(headers)) + "|"
    body = rows or [["{{待填:%s}}" % h for h in headers]]
    lines = [head, rule] + ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def _known_device_rows(facts: Any, headers: list[str]) -> list[list[str]]:
    """报价表里的设备名称与数量是招标文件给的确定值，可以预填；价格一律待填。"""
    rows = facts or []
    if isinstance(rows, dict):
        rows = [{"name": k, "value": v} for k, v in rows.items()]
    names = [str(r.get("value") or "").strip() for r in rows
             if isinstance(r, dict) and str(r.get("name") or "") in ("设备名称", "标的名称", "货物名称")]
    names = [n for n in names if n and len(n) <= 40]
    if not names:
        return []
    filled: list[list[str]] = []
    for index, name in enumerate(names[:6], start=1):
        row: list[str] = []
        for head in headers:
            if head in ("序号",):
                row.append(str(index))
            elif "名称" in head:
                row.append(name)
            else:
                row.append("{{待填:%s}}" % head)
        filled.append(row)
    return filled


def _can_join(previous: str, line: str) -> bool:
    """PDF 把一句话按版面折成多行时接回去（表格行、表头、注释、落款都不并）。"""
    if _PLACEHOLDER in previous or _PLACEHOLDER in line:
        return False
    if _is_tail_line(previous) or _is_tail_line(line):
        return False
    # 编号注释行（「2、本次报价为不含税价」）各自成行，不能被上一句吞掉：
    # 一旦并进上一句，就再也认不出它是表下注释了。
    if _NUMBERED_NOTE.match(line):
        return False
    # 上一行是表头：下一行是数据行，不能并进表头
    if split_columns(previous) or _names_only_columns(previous):
        return False
    # 本行是折行表头或数据行：它们各自成行，由表格还原接管
    if _continuation_columns(line) or _is_data_row(line, 8):
        return False
    # 上一行已经是数据行（「2 第五章 2.1 …」）：不能再并进表下的说明句
    if _is_data_row(previous, 8):
        return False
    return (len(previous) >= 18 and len(line) > 8
            and not previous.endswith(("。", "；", "：", "！", "？", "|", "）", "】"))
            and not line.startswith(("（", "(", "注：", "注:", "附", "序号")))


def _clean_lines(body: str) -> list[str]:
    merged: list[str] = []
    for raw in body.splitlines():
        line = raw.strip(" 　\t")
        # PDF 页脚页码（单独一行 1~3 位数字）不是正文，照抄会把「52」抄进标书
        if not line or re.fullmatch(r"\d{1,3}", line) or re.fullmatch(r"第\s*\d+\s*页", line):
            continue
        # 整行只有省略号/点线（投标函「（13）其他资料」下面的「……」）是招标文件里的
        # 空白占位线，照录到成稿里像没写完，直接丢掉；表格行不会命中。
        if is_ellipsis_line(line):
            continue
        if merged and _can_join(merged[-1], line):
            merged[-1] = merged[-1] + line
            continue
        merged.append(line)
    return merged


def _try_table(lines: list[str], start: int) -> tuple[dict[str, Any], int] | None:
    """从 start 行起还原一张表：表头（含折行续行）+ 数据行（含占位行）。"""
    headers = split_columns(lines[start])
    form_header = not headers
    if form_header:
        headers = _names_only_columns(lines[start])
    if not headers:
        return None
    rows: list[list[str]] = []
    notes: list[str] = []
    # 资格表的列名行后面跟的就是数据行（「投标人名称 注册地址 …」下一行是值）
    data_mode = form_header
    # 上一行是不是数据行：「合计」行只有紧跟数据行时才并表（表头后面的不算）
    previous_data = False
    total_at = -1
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if _PLACEHOLDER in line:
            rows.append(_simple_tokens(line))
            data_mode = True
            previous_data = True
            index += 1
            continue
        if not data_mode:
            # 折行表头：结构判断（不看提示词）+ 正好补齐剩余列 + 下一行是数据行
            follower = lines[index + 1] if index + 1 < len(lines) else ""
            continuation = _header_continuation(line, headers, follower)
            if continuation:
                headers.extend(continuation)   # 折行表头并回同一表头，列数含续行
                previous_data = False
                index += 1
                continue
        if _is_data_row(line, len(headers)):
            rows.append(_simple_tokens(line))
            data_mode = True
            previous_data = True
            index += 1
            continue
        # 「合计」行必须并进表格最后一行，不能掉到表外成段落（列数不齐由 _fit_row 补齐）
        if previous_data and _is_total_row(line, len(headers)):
            rows.append(_simple_tokens(line))
            total_at = len(rows) - 1
            previous_data = False
            index += 1
            continue
        # 注释行 /「说明：…」排到表格下方：既不能并进表头，也不能把表格切断
        if (_NOTE_LINE.match(line) or _EXPLAIN_LINE.match(line)) and not _is_sign_line(line):
            notes.append(line)
            previous_data = False
            index += 1
            continue
        if data_mode and _looks_like_row(line, len(headers)):
            rows.append(_simple_tokens(line))
            previous_data = True
            index += 1
            continue
        break
    if total_at >= 0 and total_at != len(rows) - 1:
        rows.append(rows.pop(total_at))        # 合计行永远是表格最后一行
    return {"kind": "table", "headers": headers, "rows": rows,
            "notes": notes, "form": form_header}, index


def _try_pair_table(lines: list[str], start: int) -> tuple[dict[str, Any], int] | None:
    """资格审查类的竖排行组（「列名：值」）还原成两列表格。"""
    rows, index = _pair_rows(lines, start)
    if len(rows) < 2:
        return None
    # 原文没给表头，不替招标人编一个：表头留空，空白格保留成空格
    return {"kind": "table", "headers": ["", ""], "rows": rows,
            "notes": [], "form": True}, index


def _fit_row(cells: list[str], columns: int) -> list[str]:
    """列数以表头为准：多出来的并进最后一格，缺的补空格。"""
    row = [str(cell) for cell in cells]
    if columns <= 0:
        return row
    if len(row) > columns:
        row = row[:columns - 1] + [" ".join(row[columns - 1:])]
    row.extend([" "] * (columns - len(row)))
    return row


def _render_block(block: dict[str, Any], facts: Any) -> str:
    if block["kind"] == "text":
        return block["text"]
    headers = block["headers"]
    # 资格表的「投标人名称」不是设备名称：台账事实只预填报价/清单表，不乱填资格表
    rows = block["rows"] or ([] if block.get("form") else _known_device_rows(facts, headers))
    if rows:
        table = markdown_table(headers, [_fit_row(row, len(headers)) for row in rows])
    elif block.get("form"):
        # 资格表给了列名没给值：空白格保留成空格，不套 {{待填:…}}
        table = markdown_table(headers, [[" "] * len(headers)])
    else:
        table = markdown_table(headers)
    notes = block.get("notes") or []
    return table if not notes else table + "\n\n" + "\n\n".join(notes)


def _build_blocks(lines: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        block = _try_table(lines, index)
        if block is None:
            block = _try_pair_table(lines, index)
        if block is not None:
            blocks.append(block[0])
            index = block[1]
            continue
        blocks.append({"kind": "text", "text": lines[index]})
        index += 1
    return blocks


def _notes_after_tables(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """注释行与落款不能出现在表格上方：把表格前面那几行挪到表格下方。

    表格下方本来就有注释/落款时，挪过来的和原地的合并成一段：注释在前、
    落款在后，同一类里保持原文顺序（「2、本次报价…」不能被排到落款后面）。
    """
    first = next((i for i, block in enumerate(blocks) if block["kind"] == "table"), None)
    if first is None:
        return blocks
    moved = [b for b in blocks[:first] if b["kind"] == "text" and _tail_rank(b["text"]) >= 0]
    if not moved:
        return blocks
    head = [b for b in blocks[:first] if not (b["kind"] == "text" and _tail_rank(b["text"]) >= 0)]
    rest = blocks[first:]
    end = 0
    while end < len(rest) and rest[end]["kind"] == "table":
        end += 1
    # 紧跟在表格后面的注释/落款块：与表前挪过来的合并，不能夹在表格和落款中间
    kept = 0
    while end + kept < len(rest):
        block = rest[end + kept]
        if block["kind"] == "text" and _tail_rank(block["text"]) >= 0:
            kept += 1
            continue
        break
    tail = moved + rest[end:end + kept]
    tail.sort(key=lambda b: _tail_rank(b["text"]))   # 稳定排序：注释(0) 在落款(1) 前
    return head + rest[:end] + tail + rest[end + kept:]


def _render_structured(tables: Any) -> str:
    """结构化表格 → 正文（延迟导入：解析层依赖 pdfplumber，不装也要能出件）。"""
    from zhibiao.core.structured_forms import render_structured_section

    return render_structured_section(tables)


def _richer_body(structured: str, section: dict[str, Any], facts: Any,
                 count: int) -> tuple[str, dict[str, Any]]:
    """结构化出稿后与启发式出稿比信息量：直出 / 合并 / 降级（绝不允许更薄）。

    三条出路：
    - 结构化没丢文字 → 直出（`source="structured"`，与历史行为完全一致）；
    - 结构化变薄（表画出来了，表外正文被掏空）→ **行级合并**（`source="merged"`）：
      保留结构化画出的表格块，把启发式里"表格没覆盖到"的文字行按原顺序补回来；
    - 合并也补不回来 → 整节退回启发式（`source="heuristic"`，`degraded=True`，
      `reason="structured_thinner_than_heuristic"`）。

    为什么优先"合并"而不是二选一：两条路各有一半是对的 —— 表格的列数/合并/合计位置
    只有版式知道（启发式照着两个空格猜列，历史错位全栽在这里），而"这一节还有哪些字"
    只有原文正文知道（结构化输出只带表前后紧邻的几行）。二选一必然丢掉一半；
    "表按版式、字按原文"的合并才是既保住结构化又保住正文的唯一口径。
    唯一不能退让的是：成稿的信息量不比旧路径少，所以合并后还要再比一次，
    仍比启发式薄就整节退回启发式。
    """
    from zhibiao.core.structured_forms import (is_structured_thinner_than_heuristic,
                                           merge_structured_with_heuristic,
                                           missing_text_lines)

    heuristic = render_section_body(section, facts)
    plain = {"source": "structured", "degraded": False, "reason": "", "tables": count}
    if not is_structured_thinner_than_heuristic(structured, heuristic):
        return structured, plain
    recovered = missing_text_lines(structured, heuristic)
    try:
        merged = merge_structured_with_heuristic(structured, heuristic)
    except Exception:  # noqa: BLE001 - 合并失败不能挡住出稿，退回启发式即可
        merged = ""
    if merged.strip() and not is_structured_thinner_than_heuristic(merged, heuristic):
        return merged.strip(), {"source": "merged", "degraded": False, "reason": "",
                                "tables": count, "merged_lines": len(recovered)}
    if heuristic.strip():
        return heuristic.strip(), {"source": "heuristic", "degraded": True,
                                   "reason": "structured_thinner_than_heuristic",
                                   "tables": count}
    return structured, plain


def render_section_body_ex(section: dict[str, Any], facts: Any = None, *,
                           structure: Any = None) -> tuple[str, dict[str, Any]]:
    """渲染一节格式正文，并说明走的是哪条路（structured / merged / heuristic）。

    `structure` 是 `structured_forms.structure_for_section(...)` 的结果，可以是 None。
    - 有结构化表 → 直出（列数、合并、合计位置、注释归属都来自结构，不需要猜）；
      直出结果与启发式比过信息量：比启发式薄就**合并**（表按版式、字按原文），
      合并也补不回来才整节降级 —— 详见 `_richer_body`；
    - 拿不到 → 走下面那套一行未改的启发式，并在元信息里标 `degraded=True` + 原因。

    元信息给调用方（阶段 3 的生成链路）写进章节来源，界面上就能看出这一节
    到底是"结构化还原""结构化+原文正文合并"还是"文本启发式"，出了问题不必再靠猜。
    幂等：同一节同一 structure 反复调用结果逐字相同（合并只读原文，不累积）。
    """
    meta: dict[str, Any] = {"source": "heuristic", "degraded": True, "reason": "", "tables": 0}
    if structure:
        tables = list(structure.get("tables") or [])
        if structure.get("ok") and tables:
            try:
                rendered = _render_structured(tables)
            except Exception:  # noqa: BLE001 - 结构化渲染失败必须退回启发式，不能空章
                rendered = ""
            if str(rendered or "").strip():
                return _richer_body(str(rendered).strip(), section, facts, len(tables))
            meta["reason"] = "render_empty"
        else:
            meta["reason"] = str(structure.get("reason") or "no_tables_for_section")
    else:
        meta["reason"] = "no_structure"
    return render_section_body(section, facts), meta


def render_section_body(section: dict[str, Any], facts: Any = None) -> str:
    """把一节格式原文渲染成正文：文字照抄，表头还原成表格。"""
    body = str(section.get("body") or "").strip()
    if len(body) < _MIN_BODY:
        return ""
    blocks = _build_blocks(_clean_lines(body))
    text = "\n\n".join(_render_block(block, facts) for block in _notes_after_tables(blocks))
    # PDF 里「年 月 日」中的空格保留原样（是填写位），但「一 年」这类量词空格要收掉
    # 只收中文数词与量词之间的空格；「第 1.4.3 项」「11 万元」是招标原文排版，保持原样
    text = re.sub(r"(?<=[一二三四五六七八九十两])\s+(?=[年月日天台件套个条项只组张份])", "", text)
    return text.strip()


_TOC_LEFTOVER = re.compile(r"(?:\.{4,}|…{2,})\s*\d{1,3}\s*$", re.M)


def validate_format_chapter(text: str, title: str = "") -> list[str]:
    """格式章节（照录招标原文）的专用校验。

    这类章节的内容不是平台写出来的，而是招标人规定的固定格式：用「断尾/篇幅/偏差声明/
    否决措辞/编号体系」这些**写作质量**规则去卡它，等于用作文评分标准去判一张空白表格 ——
    实测直接把法定代表人身份证明、授权委托书、商务和技术偏差表、分项报价表四章判成失败。
    所以这里只保留「照录件本身是否可用」的检查：有没有内容、有没有把目录残留抄进来。
    """
    body = str(text or "").strip()
    if not body:
        return ["招标文件「投标文件格式」章里没有取到本节格式原文，需人工确认格式或补充招标文件"]
    if len(re.sub(r"\s", "", body)) < 20:
        return ["格式原文过短（不足 20 字），可能只解析到标题、没取到正文"]
    if _TOC_LEFTOVER.search(body):
        return ["格式原文里混入了目录点线与页码，需人工核对本节格式"]
    return []


def format_chapter_text_ex(title: str, tender_text: str = "", facts: Any = None, *,
                           structure: Any = None) -> tuple[str | None, dict[str, Any]]:
    """`format_chapter_text` 的带元信息版本：返回 (正文, {source, degraded, reason, tables})。

    正文为 None 时同样给出 reason（`no_section` / `authored_section` / `no_source`），
    这样"这一节为什么没出稿"和"出稿用的是哪条路"都能被记录，不用再猜。
    """
    from zhibiao.core.tender_checkup import format_document_section

    meta: dict[str, Any] = {"source": "none", "degraded": True, "reason": "", "tables": 0}
    if not str(tender_text or "").strip() or not str(title or "").strip():
        meta["reason"] = "no_source"
        return None, meta
    if requires_authored_content(title):
        # 技术/服务类小节招标文件只给白纸，必须由模型按项目实际写，照抄等于交白卷
        meta["reason"] = "authored_section"
        return None, meta
    section = format_document_section(tender_text, title)
    if not section:
        meta["reason"] = "no_section"
        return None, meta
    if requires_authored_content(title, str(section.get('body') or '')):
        meta['reason']='authored_section'
        return None, meta
    rendered, detail = render_section_body_ex(section, facts, structure=structure)
    if not rendered:
        detail = {**detail, "degraded": True, "reason": detail.get("reason") or "empty_body"}
        return None, detail
    # 同级小节一个都不能少：结构化直出只保留检出网格的表，没有表格的小节（资格审查资料
    # 的（二）（五）（六）（八））会被整段丢掉 —— 补全必须以「招标原文有哪几个小节」为准。
    # 齐全时逐字返回，重复执行不会重复生成（幂等）。
    from zhibiao.core.format_subsections import ensure_format_subsections
    # 响应函/投标函这类「函件」的骨架要先补：原文常把承诺条款写成待填留白，
    # 直接把原文照录下来就是一堆下划线；补骨架会把条款条数、报价与下浮率两个变量、
    # 签章落款按原文补齐（只在真的是函件、且原文没有现成条款时才动手，幂等）。
    from zhibiao.core.format_subsections import ensure_response_letter_frame
    rendered, letter_note = ensure_response_letter_frame(rendered, tender_text, title)
    if letter_note:
        detail = {**detail, "response_letter": letter_note}
    rendered, added = ensure_format_subsections(rendered, tender_text, title)
    if added:
        detail = {**detail, "subsections_added": added}
    # 正文只放格式本身：说明文字写进章节来源（界面可见），不塞进标书，免得交给评委的文件里
    # 出现「说明：以上格式照录…」这种系统口吻（用户已明确反馈这是失误）。
    return rendered, detail


def format_chapter_text(title: str, tender_text: str = "", facts: Any = None, *,
                        structure: Any = None) -> str | None:
    """按招标文件格式章给这一章出稿；没有对应格式原文返回 None。

    只有**文书/表格类**小节才走这条路：技术描述、技术支持、服务计划这些招标文件
    只给一张白纸，必须由模型按项目实际内容写，照抄等于交白卷。

    `structure` 给定时优先走结构化表格直出（见 `render_section_body_ex`）；
    不传则行为与历史完全一致。
    """
    return format_chapter_text_ex(title, tender_text, facts, structure=structure)[0]
