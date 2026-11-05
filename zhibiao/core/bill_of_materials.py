"""清单抽取：随机备件/备品备件/专用工具明细、关键部件数量行。

线上事故（第一份6，唐安端头支架）：
* 招标文件第63～65页「随机备件明细」共 24 项，正文只列了 12 项，漏掉单向锁、双向锁、
  球形截止阀、回液断路器、双针压力表、主进/回液胶管、电液换向阀芯、电磁先导阀、
  U型卡专用工具；端尾支架漏了立柱 1 根、小锤 1 件、拔销器 M24 1 件。
* 千斤顶数量在招标原文是「2 根/组」「4 根/组」，正文写成「按组配套」——数量口径丢失。

根因：`render_device_parameters` 只收「主要技术参数」表，备件明细表根本没进上下文；
而 PDF 文本在第 63/64 页之间被分页切断、表头重复出现，行也被拆成「序号 名称 型号 单位 数量」
的散列。模型只能凭印象写，于是写成 12 项、数量写成「按组配套」。

本模块把这些表抽成**逐项清单**注入上下文，让「照抄」成为可能：
* 清单：`序号 名称 规格 单位 数量`（支持管道表格行与空格分隔行，跨页续行自动拼接）；
* 数量行：名称含千斤顶/立柱、参数为「数量」的行（`4 根/组`）。
"""

from __future__ import annotations

import re
from typing import Any

_UNITS = r"(?:根/组|根|件|套|个|批|台|组|支|条|把|只|块|张|米|m|kg)"
_UNIT_CAP = r"(" + _UNITS[3:-1] + r")"
# 清单表行（管道格式）：| 8 | 单向锁 | FDD200\50 | 件 | 6 | 备注 |
# 注意 PDF 抽出来的第一行常常没有前导竖线（`3 | 推移连接头 |  | 件 | 2`），必须允许缺省。
_PIPE_ROW = re.compile(
    r"^\s*\|?\s*(\d{1,2})\s*\|\s*([^|\n]{1,24}?)\s*\|\s*([^|\n]{0,40}?)\s*\|\s*" + _UNITS + r"\s*\|\s*(\d{1,3})\s*\|"
)
# 清单表行（空格格式）：1 立柱 DH(31.5)-835/160S 根 4
_SPACE_ROW = re.compile(
    r"^\s*(\d{1,2})\s+([\u4e00-\u9fa5A-Za-zＵU][^\s|]{1,23})\s+([^\s|]{0,28})\s+" + _UNITS + r"\s+(\d{1,3})(?:\s|$)"
)
_LIST_HEAD = re.compile(r"^.{0,40}(随机备件|备品备件|备件明细|专用工具|随机附件)")
# 部件数量行：5 伸缩梁千斤顶 数量 4 根/组  /  | 5 | 伸缩梁千斤顶 | 数量 | 4 | 根/组 |
_QTY_PIPE = re.compile(
    r"^\s*\|?\s*\d{0,3}\s*\|?\s*([^|\n]{0,20}?(?:千斤顶|立柱))[^|\n]{0,10}?\|\s*数量\s*\|\s*(\d{1,3})\s*\|\s*" + _UNIT_CAP
)
_QTY_SPACE = re.compile(
    r"^\s*(?:\d{1,2}\s+)?([\u4e00-\u9fa5]{0,14}?(?:千斤顶|立柱))\s+数量\s+(\d{1,3})\s+" + _UNIT_CAP
)


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "")).strip("：:、|")


_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])")


def _compact_line(line: str) -> str:
    """PDF 会把「伸缩梁千斤顶」拆成「伸缩 梁千 斤顶」，汉字之间的空格要先合上，
    否则名称匹配不到（实测备件/千斤顶数量行就是这样漏掉的）。"""
    return _CJK_GAP.sub("", line)


def _match_any(patterns: tuple[re.Pattern[str], ...], line: str) -> re.Match[str] | None:
    # Word-extracted blank/indented lines can contain thousands of spaces.
    # Adjacent optional whitespace groups in the row regexes otherwise explore
    # combinatorially many partitions before rejecting them. These patterns only
    # need separators, not their width; normalize once and skip blank rows.
    line = " ".join(line.split())
    if not line:
        return None
    for candidate in (line, _compact_line(line)):
        for pattern in patterns:
            found = pattern.match(candidate)
            if found:
                return found
    return None


def _rows(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in str(text or "").splitlines():
        line = raw.replace("｜", "|")
        match = _match_any((_PIPE_ROW, _SPACE_ROW), line)
        if not match:
            continue
        index, name, spec, qty = match.group(1), _clean_name(match.group(2)), _clean_name(match.group(3)), match.group(4)
        if not name or name in {"序号", "名称", "规格型号", "备注"}:
            continue
        key = (name, spec)
        if key in seen:
            continue
        seen.add(key)
        out.append({"index": index, "name": name, "spec": spec, "qty": qty})
    return out


def _list_heads(text: str) -> list[tuple[int, str]]:
    heads: list[tuple[int, str]] = []
    for index, line in enumerate(str(text or "").splitlines()):
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            continue
        if _LIST_HEAD.search(stripped) and ("：" in stripped or ":" in stripped or stripped.endswith("明细")):
            heads.append((index, stripped.rstrip("：:")))
    return heads


def extract_spare_parts(tender_text: str) -> list[dict[str, Any]]:
    """按「随机备件明细」标题切段，抽出每份清单的逐项条目。"""
    lines = str(tender_text or "").splitlines()
    heads = _list_heads(tender_text)
    if not heads:
        return []
    groups: list[dict[str, Any]] = []
    prev_max: int | None = None
    for pos, (index, title) in enumerate(heads):
        end = heads[pos + 1][0] if pos + 1 < len(heads) else min(len(lines), index + 400)
        rows = _rows("\n".join(lines[index:end]))
        # 太短的"清单"多半是正文里顺带提到的一句话，不是表
        if len(rows) < 4:
            continue
        # PDF 分页会把上一份清单的末尾几行排到下一个标题下面（唐安：第 65 页的 22～24 项
        # 落在「端尾支架随机备件明细」标题之后）。序号能接上前一份的末号，就归回前一份。
        carried: list[dict[str, Any]] = []
        if prev_max is not None and groups:
            # 同一份清单的「1..prev_max」是本标题自己的；比 prev_max 大的序号是上一份清单的分页续行
            own_rows = [r for r in rows if r["index"].isdigit() and int(r["index"]) <= prev_max]
            spill = [r for r in rows if r not in own_rows]
            if own_rows and any(r["index"] == "1" for r in own_rows) and spill:
                known = {(row["name"], row["spec"]) for row in groups[-1]["items"]}
                for row in spill:
                    if (row["name"], row["spec"]) not in known:
                        groups[-1]["items"].append(row)
                        known.add((row["name"], row["spec"]))
                rows = own_rows
                carried = spill
        while rows and prev_max is not None and rows[0]["index"].isdigit() \
                and int(rows[0]["index"]) == prev_max + 1 and not carried:
            carried.append(rows.pop(0))
        if carried and groups:
            known = {(row["name"], row["spec"]) for row in groups[-1]["items"]}
            for row in carried:
                if (row["name"], row["spec"]) not in known:
                    groups[-1]["items"].append(row)
                    known.add((row["name"], row["spec"]))
            prev_max = int(groups[-1]["items"][-1]["index"]) if groups[-1]["items"][-1]["index"].isdigit() else prev_max
        if len(rows) < 3:
            continue
        # 上一份清单的分页续行（唐安第 65 页的 22～24 项）会排到下一个标题下面，
        # 这些行在上一组里已经有了，按「名称+规格」去重，避免两端清单互相污染。
        if groups:
            known = {(row["name"], row["spec"]) for row in groups[-1]["items"]}
            rows = [row for row in rows if (row["name"], row["spec"]) not in known]
            if len(rows) < 3:
                continue
        groups.append({"title": re.sub(r"^.*?(?=[（(]?\d?[)）]?\S*(?:随机|备品|备件|专用))", "", title)[:40] or title,
                       "items": rows})
        last = rows[-1]["index"]
        prev_max = int(last) if last.isdigit() else prev_max
    # 同一份清单在 PDF 里可能被分页重复（表头再来一次），合并同名清单并去重
    merged: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for group in groups:
        key = re.sub(r"[（(].*?[)）]", "", group["title"])[:18]
        if key not in merged:
            merged[key] = []
            order.append(key)
        known = {(row["name"], row["spec"]) for row in merged[key]}
        for row in group["items"]:
            if (row["name"], row["spec"]) not in known:
                merged[key].append(row)
                known.add((row["name"], row["spec"]))
    return [{"title": key, "items": merged[key]} for key in order if len(merged[key]) >= 3]


def extract_part_quantities(tender_text: str) -> list[dict[str, str]]:
    """抽出「名称 + 数量 + 单位」的部件行（千斤顶/立柱），供正文照抄数量口径。

    数量必须带设备归属：端头 与 超前 的同一个千斤顶数量不同（实测立柱 20/16、侧推 12/8），
    不归属就会把数字配错设备 —— 那正是「按组配套」之后的下一个废标点。
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    device = ""
    for raw in str(tender_text or "").splitlines():
        line = raw.replace("｜", "|")
        stripped = line.strip()
        if stripped and len(stripped) <= 40:
            head = re.match(r"^(?:\d+(?:\.\d+)?\s*)?(端头|超前|端尾|中置式大阻力端尾)[^。，]{0,12}?(?:支架|立柱)", stripped)
            if head:
                device = head.group(1)
        match = _match_any((_QTY_PIPE, _QTY_SPACE), line)
        if not match:
            continue
        name, qty = _clean_name(match.group(1)), match.group(2)
        unit = match.group(3)
        key = (device, name, qty)
        if name and key not in seen:
            seen.add(key)
            out.append({"device": device, "name": name, "qty": qty, "unit": unit})
    return out


def render_bill_context(tender_text: str, max_items: int = 40) -> str:
    """渲染成注入提示词的清单基线。没有清单时返回空串（不改变既有行为）。"""
    groups = extract_spare_parts(tender_text)
    quantities = extract_part_quantities(tender_text)
    if not groups and not quantities:
        return ""
    parts: list[str] = ["【清单基线（招标原文表，逐项照抄，不得合并或省略）】"]
    if groups:
        parts.append("以下随机备件/备品备件明细必须**逐项列出**：序号、名称、规格型号、单位、数量"
                     "全部照原文，不得只列一部分，不得写「按组配套」「若干」「详见附表」代替数量。")
    for group in groups:
        items = sorted(group["items"][:max_items], key=lambda row: int(row["index"]) if str(row["index"]).isdigit() else 99)
        rendered = "；".join(
            f"{row['index']} {row['name']}{(' ' + row['spec']) if row['spec'] else ''} {row['qty']}{_unit_of(row)}"
            for row in items
        )
        tail = f"（共 {len(group['items'])} 项）"
        parts.append(f"· {group['title']}{tail}：{rendered}")
    if quantities:
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in quantities[:40]:
            grouped.setdefault(row.get("device") or "本项目", []).append(row)
        for device, rows in grouped.items():
            rendered = "；".join(f"{row['name']} {row['qty']}{row['unit']}" for row in rows)
            parts.append(f"· {device}：{rendered}")
    parts.append("数量口径要求：上述数量照原文写（如「2 根/组」），严禁改写成「按组配套」「若干」「见清单」。")
    return "\n".join(parts)


def _unit_of(row: dict[str, Any]) -> str:
    return str(row.get("unit") or "")


def _spec_usable(spec: str) -> bool:
    """规格型号能否作为「这一项写过」的证据。

    PDF 里不少条目没有规格，抽出来是「\\」「—」「/」这类占位符：拿它们去比对，
    「\\」在正文任何一处（如 FDD200\\50）都能命中，等于把缺项判成已写。
    """
    text = str(spec or "").strip()
    return len(text) >= 3 and text not in {"—", "-", "–", "/", "＼", "\\", "无"}


def missing_spare_items(tender_text: str, body: str) -> list[str]:
    """正文漏掉了哪些清单条目（用于质检：只报名称，提示逐项补全）。"""
    groups = extract_spare_parts(tender_text)
    if not groups:
        return []
    flat = re.sub(r"\s+", "", str(body or ""))
    missing: list[str] = []
    for group in groups:
        for row in group["items"]:
            name = row["name"]
            # 规格型号能对上就算写了（名称可能被改写成同义表述），占位符规格不算证据
            spec = row["spec"]
            if name in flat or (_spec_usable(spec) and spec in flat):
                continue
            missing.append(name)
    return list(dict.fromkeys(missing))


def spare_table_gaps(tender_text: str, body: str) -> list[str]:
    """按「每一份清单」核对缺项，缺一项也算缺（不再用 30% 阈值放行）。

    线上事故（V7）：
    * 端头/超前随机备件列了 1～23 项，漏第 24 项「U型卡专用工具 个 6」；
    * 端尾支架表列了 2/4/6/7/8 项，跳掉 1（立柱 1 根）、3（推移连接头 2 件）、5（小锤 1 件）。
    旧判定要求「漏项数 ≥ max(3, 全部清单项数×30%)」：24 项漏 1 项、8 项漏 3 项都够不到
    阈值，于是整表截断与跳号被全部放行。备件是报价与响应范围的一部分，缺一项就是漏项，
    阈值改成「该清单已在正文里列了 ≥3 项，且仍有缺项就报」。
    """
    groups = extract_spare_parts(tender_text)
    flat = re.sub(r"\s+", "", str(body or ""))
    gaps: list[str] = []
    for group in groups:
        present = 0
        missing: list[str] = []
        for row in group["items"]:
            name = row["name"]
            spec = row["spec"]
            if (name and name in flat) or (_spec_usable(spec) and spec in flat):
                present += 1
            else:
                missing.append(f"{row['index']} {name}")
        if present >= 3 and missing:
            head = "、".join(missing[:8]) + (f" 等 {len(missing)} 项" if len(missing) > 8 else "")
            gaps.append(f"{group['title']}缺 {len(missing)} 项：{head}")
    return gaps
