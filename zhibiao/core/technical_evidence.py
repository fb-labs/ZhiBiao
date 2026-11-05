"""保留 PDF 原表的分组和单元格归属，供生成和核查使用。"""
from __future__ import annotations

import re
from typing import Any

_GROUP = re.compile(r"^\s*([^\d，。:：]{2,30}?)\s*(\d+)\s*(架|台|套|组|辆|部|座|项)\s*$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _cell(table: dict, row: int, col: int) -> str:
    for cell in table.get("cells", []):
        if cell.get("col") == col and cell.get("row", -1) <= row < cell.get("row", -1) + cell.get("rowspan", 1):
            return str(cell.get("text") or "").strip()
    return ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _owner_name(value: str) -> str:
    return re.sub(r"[（(︵].*$", "", _clean(value))


def extract_technical_evidence(report: dict[str, Any]) -> list[dict[str, Any]]:
    """只绑定能从原表合并单元格和明确设备标题确定的行。"""
    if not report.get("ok"):
        return []
    pages = report.get("page_texts") or {}
    tables = sorted(report.get("tables") or [], key=lambda t: (t.get("page", 0), (t.get("bbox") or [0, 0])[1]))
    groups: dict[int, tuple[str, str]] = {}
    current = None
    current_page = -10
    for page in sorted(int(x) for x in pages):
        page_text = pages.get(str(page), pages.get(page, "")) or ""
        page_lines = page_text.splitlines()
        for index, line in enumerate(page_lines):
            match = _GROUP.match(line)
            nearby = "".join(page_lines[index + 1:index + 5])
            if (match and re.search(r"技术参数|技术要求|规格参数", nearby)
                    and not re.search(r"其中|合计|共计|第[一二三四五六七八九十\d]+标段", match.group(1))):
                current = (_clean(match.group(1)), match.group(2) + match.group(3))
                current_page = page
        if current and (page - current_page <= 1 or (page - current_page == 2 and any(t.get("page") == page - 1 for t in tables))):
            groups[page] = current
    result = []
    previous_table = None
    for table in tables:
        page = table.get("page")
        group = groups.get(page)
        if not group:
            continue
        rows = table.get("rows") or []
        cols = table.get("n_cols") or 0
        # The two supported table shapes have explicit semantic column headers,
        # or component rows with a metric in column 2. Other layouts are skipped.
        if cols == 4:
            previous_prefix = ""
            first_owner = _cell(table, 0, 0)
            if previous_table and previous_table.get("page") == page - 1 and groups.get(page - 1) == group and previous_table.get("n_cols") == 4:
                prev_rows = previous_table.get("rows") or []
                tail = _clean(_cell(previous_table, len(prev_rows) - 1, 0)) if prev_rows else ""
                if len(tail) == 1 and first_owner and len(_clean(first_owner)) >= 2:
                    previous_prefix = tail
            for i, row in enumerate(rows):
                if len(row) < 4:
                    continue
                owner, metric, value, unit = _cell(table, i, 0), *[str(x or "").strip() for x in row[1:4]]
                if previous_prefix and owner == first_owner:
                    owner = previous_prefix + owner
                if len(_clean(owner)) < 2 or _clean(owner) in ("千斤顶", "液压缸") or not metric or not value or metric.replace(" ", "") in ("名称", "项目"):
                    continue
                result.append(dict(group=group[0], quantity=group[1], owner=_owner_name(owner), metric=_clean(metric), value=value, unit=unit, page=page))
        elif cols >= 5:
            assembly = bool(rows and len(rows[0]) >= 5 and _clean(str(rows[0][1])) in ("项目", "名称", "参数"))
            carry_owner = ""
            if previous_table and previous_table.get("page") == page - 1 and groups.get(page - 1) == group and previous_table.get("n_cols") == cols:
                prev_rows = previous_table.get("rows") or []
                if prev_rows:
                    carry_owner = _cell(previous_table, len(prev_rows) - 1, 1)
            for i, row in enumerate(rows):
                if len(row) < 5:
                    continue
                owner = _cell(table, i, 1)
                if not owner and carry_owner and not any(_cell(table, j, 1) for j in range(i + 1)):
                    owner = carry_owner
                metric, value, unit = (str(row[j] or "").strip() for j in (2, 3, 4))
                if owner and metric == "型式" and len(row) > 5:
                    quantity = re.fullmatch(r"\s*(\d+)\s*(根|个|件|台|套|组)\s*", str(row[5] or ""))
                    if quantity:
                        result.append(dict(group=group[0], quantity=group[1], owner=_clean(owner), metric="数量", value=quantity.group(1), unit=quantity.group(2), page=page))
                if not owner or not value or not unit:
                    continue
                if assembly:
                    if owner in ("项目", "名称", "参数"):
                        continue
                    result.append(dict(group=group[0], quantity=group[1], owner=group[0], metric=_clean(owner + metric), value=value, unit=unit, page=page))
                    continue
                if not metric:
                    continue
                # A component entry has a rowspan covering multiple parameter rows.
                span = next((c.get("rowspan", 1) for c in table.get("cells", []) if c.get("col") == 1 and c.get("row", -1) <= i < c.get("row", -1) + c.get("rowspan", 1)), 1)
                if (span < 2 and not (carry_owner and owner == carry_owner)) or metric in ("最低", "最高", "最小", "最大"):
                    continue
                result.append(dict(group=group[0], quantity=group[1], owner=_clean(owner), metric=_clean(metric), value=value, unit=unit, page=page))
        previous_table = table
    return result


def render_technical_evidence(evidence: list[dict], max_chars: int = 18000) -> str:
    if not evidence:
        return ""
    lines = ["【原始技术表分组依据】以下为招标原文表格，按设备名称、数量、部件及参数绑定；不同组、不同部件不得互换。无法确定归属时须待核，不得猜填。"]
    for item in evidence:
        line = "PDF第%s页｜%s %s｜%s｜%s｜%s %s" % (item["page"], item["group"], item["quantity"], item["owner"], item["metric"], item["value"], item["unit"])
        if sum(map(len, lines)) + len(line) > max_chars:
            lines.append("（其余原表行未展示，须查阅原件）")
            break
        lines.append(line)
    return "\n".join(lines)


def render_grouped_source_clauses(report: dict[str, Any], max_per_group: int = 6000) -> str:
    """把原文结构、工艺等条款与其所在设备标题一起提供给写作模型。"""
    pages = (report or {}).get("page_texts") or {}
    grouped: dict[tuple[str, str], list[tuple[int, str]]] = {}
    current = None
    for page in sorted(int(x) for x in pages):
        lines = str(pages.get(str(page), pages.get(page, "")) or "").splitlines()
        for i, line in enumerate(lines):
            match = _GROUP.match(line)
            if match and re.search(r"技术参数|技术要求|规格参数", "".join(lines[i + 1:i + 5])):
                current = (_clean(match.group(1)), match.group(2) + match.group(3))
            elif re.match(r"^\s*第[一二三四五六七八九十\d]+章\s+", line):
                current = None
            if current and re.search(r"结构|工艺|处理|涂装|材质|材料|阀|支护板|推移", line):
                context = " ".join(_clean(x) for x in lines[max(0, i - 1):min(len(lines), i + 3)])
                if context:
                    grouped.setdefault(current, []).append((page, context))
    out = []
    for (name, qty), clauses in grouped.items():
        kept = []
        size = 0
        seen = set()
        for page, clause in clauses:
            if clause in seen:
                continue
            line = "PDF第%s页：%s" % (page, clause)
            if size + len(line) > max_per_group:
                break
            kept.append(line); seen.add(clause); size += len(line)
        if kept:
            out.append("【%s %s 原文结构与工艺】\n%s" % (name, qty, "\n".join(kept)))
    return "\n\n".join(out)


def quantity_breakdown_issues(text: str) -> list[str]:
    """检查明确列举的总量和组成项；不对部分清单、区间或嵌套清单猜测。"""
    issues = []
    pattern = re.compile(r"(?:合计|共|总计)\s*(\d+)\s*(架|台|套|组|辆|部|座|项)[^。；\n]{0,25}?其中([^。；\n]+)")
    for match in pattern.finditer(str(text or "")):
        total, unit, detail = int(match.group(1)), match.group(2), match.group(3)
        if re.search(r"其中|等|部分|至少|不低于|不少于|另有|含但不限于", detail):
            continue
        parts = [int(n) for n in re.findall(r"(?<![\d.])(\d+)\s*" + re.escape(unit), detail)]
        if len(parts) >= 2 and sum(parts) != total:
            issues.append("采购总量为%d%s，但明确列举的分项合计为%d%s；请按招标原文补全并核对" % (total, unit, sum(parts), unit))
    return list(dict.fromkeys(issues))


_GRADE = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}[A-Za-z][A-Za-z0-9]*|\d{3})(?![A-Za-z0-9])")


def extract_material_evidence(report: dict[str, Any]) -> list[dict[str, Any]]:
    """只读取有明确名称、材质列的原表，保留行归属和来源页。"""
    result = []
    for table in (report or {}).get("tables", []):
        rows = table.get("rows") or []
        if not rows:
            continue
        header = [_clean(str(x or "")) for x in rows[0]]
        name_cols = [i for i, x in enumerate(header) if x in ("名称", "部件名称", "零件名称")]
        grade_cols = [i for i, x in enumerate(header) if x in ("材质", "材料", "材质要求", "材料牌号")]
        if len(name_cols) != 1 or len(grade_cols) != 1:
            continue
        ni, gi = name_cols[0], grade_cols[0]
        for row in rows[1:]:
            if max(ni, gi) >= len(row):
                continue
            owner, value = _clean(str(row[ni] or "")), _clean(str(row[gi] or ""))
            if len(owner) >= 4 and _GRADE.search(value):
                result.append(dict(owner=owner, value=value, page=table.get("page")))
    return result


def material_evidence_issues(text: str, evidence: list[dict[str, Any]]) -> list[str]:
    """材料牌号明确但不一致时提示核对，不自行判定不同材质的性能等级。"""
    issues = []
    headers = []
    for line in str(text or "").splitlines():
        if "|" not in line:
            continue
        cells = [_clean(c) for c in line.strip().strip("|").split("|")]
        if any(c in ("名称", "部件名称", "零件名称") for c in cells) and any(c in ("材质", "材料", "材质要求", "材料牌号") for c in cells):
            headers = cells
            continue
        if not headers or len(cells) < len(headers):
            continue
        ni = next(i for i, c in enumerate(headers) if c in ("名称", "部件名称", "零件名称"))
        gi = next(i for i, c in enumerate(headers) if c in ("材质", "材料", "材质要求", "材料牌号"))
        owner, value = cells[ni], cells[gi]
        actual = {x.lower() for x in _GRADE.findall(value)}
        if len(owner) < 4 or not actual:
            continue
        matches = [x for x in evidence if owner in x["owner"]]
        expected_sets = {tuple(x.lower() for x in _GRADE.findall(x["value"])) for x in matches}
        if len(expected_sets) == 1 and matches:
            expected = set(next(iter(expected_sets)))
            if expected and not actual.intersection(expected):
                issues.append("%s材质牌号与原表不同，须核对适用性：原PDF第%s页为%s" % (owner, matches[0]["page"], matches[0]["value"]))
    return list(dict.fromkeys(issues))


def process_scope_issues(text: str, source: str) -> list[str]:
    """不能仅因原文某部件出现工艺，就断言整机采用同一工艺。"""
    compact_source = _clean(source)
    if not compact_source:
        return []
    pattern = re.compile(r"(?:整机|整套|整体|设备整体|支架整体)[^。；\n]{0,12}?(?:涂装|表面处理|防腐|涂层)[^。；\n]{0,12}?(?:采用|使用)([\u4e00-\u9fffA-Za-z0-9]{2,12}?)(?:表面处理|涂层|处理工艺|工艺)")
    issues = []
    for match in pattern.finditer(_clean(text)):
        process = match.group(1)
        if process in compact_source and process not in {m.group(1) for m in pattern.finditer(compact_source)}:
            issues.append("整机采用%s的工艺范围未找到对应原文，须核对原文适用对象，不能将部件工艺直接扩大到整机" % process)
    return list(dict.fromkeys(issues))


def technical_evidence_issues(text: str, evidence: list[dict]) -> list[str]:
    """对能明确识别设备分组和部件的表格行核对行程与直径。"""
    if not evidence:
        return []
    issues: list[str] = []
    groups = {(e["group"], e["quantity"]) for e in evidence}
    active = None
    combined = None
    scopes = []
    headers = []
    section_title = ""
    table_owners: set[str] = set()

    def check_component_coverage():
        if not active or not re.search(r"配置|部件清单|部件参数|零部件", section_title) or re.search(r"部分|示例|摘录|重点|示意", section_title):
            return
        source_owners = {e["owner"] for e in evidence if (e["group"], e["quantity"]) == active and e["owner"] != active[0] and re.search(r"千斤顶|立柱|部件", e["owner"])}
        covered = source_owners & table_owners
        missing = source_owners - covered
        if len(source_owners) >= 4 and len(covered) >= max(3, len(source_owners) // 2) and missing:
            issues.append("%s %s 的部件配置表未列出原表中的%s，须核对是否遗漏" % (active[0], active[1], "、".join(sorted(missing))))

    for line in str(text or "").splitlines():
        heading = re.match(r"^\s*(#{1,6})\s+", line)
        if heading:
            check_component_coverage()
            table_owners = set()
            section_title = line
            level = len(heading.group(1))
            while scopes and scopes[-1][0] >= level:
                scopes.pop()
            candidates = [(name, qty) for name, qty in groups if re.search(r"(?<!\d)" + re.escape(qty[:-1]) + r"\s*" + re.escape(qty[-1]), line) and name in _clean(line)]
            active = candidates[0] if len(candidates) == 1 else (scopes[-1][1] if scopes else None)
            combined = None
            for name in {name for name, _ in groups if name in _clean(line)}:
                quantities = [qty for group, qty in groups if group == name]
                if len(quantities) >= 2 and len({q[-1] for q in quantities}) == 1:
                    total = str(sum(int(q[:-1]) for q in quantities)) + quantities[0][-1]
                    if re.search(r"(?<!\d)" + re.escape(total[:-1]) + r"\s*" + re.escape(total[-1]), line):
                        combined = (name, total, quantities)
            if combined:
                active = None
            scopes.append((level, active))
            headers = []
            continue
        if active and "|" not in line:
            for owner in {e["owner"] for e in evidence if (e["group"], e["quantity"]) == active}:
                if owner not in _clean(line):
                    continue
                source = [e for e in evidence if (e["group"], e["quantity"]) == active and e["owner"] == owner and "缸径" in e["metric"]]
                claim = re.search(re.escape(owner) + r"[^。；\n]{0,80}?(?:缸径|柱径)[^。；\n]{0,30}?[φΦØ]([\d.]+(?:/[φΦØ]?[\d.]+)+)", _clean(line))
                if len(source) == 1 and claim:
                    actual = _NUMBER.findall(claim.group(1))
                    expected = _NUMBER.findall(source[0]["value"])
                    if expected and any(value not in expected for value in actual):
                        issues.append("%s %s 的%s缸径与原表不符：原PDF第%s页为%s%s" % (active[0], active[1], owner, source[0]["page"], source[0]["value"], source[0]["unit"]))
        if "|" not in line:
            continue
        if combined and re.search(r"高度|宽度|中心距|初撑力|工作阻力|缸径|行程", line) and re.search(r"\d", line):
            issues.append("%s %s包含多个参数组（%s），须按原表分别列出，不能用同一组参数覆盖总数" % (combined[0], combined[1], "、".join(sorted(combined[2]))))
        if not active:
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if _clean(cells[0]) in ("部件", "部件名称", "名称", "项目", "项目名称") and any(re.search(r"缸径|行程|数量", cell) for cell in cells[1:]):
            headers = cells
            continue
        label, response = _clean(cells[0]), _clean(cells[1])
        label_owner = re.sub(r"[（(].*$", "", label)
        if headers:
            table_owners.add(label_owner)
        candidates = [e for e in evidence if (e["group"], e["quantity"]) == active and e["owner"] == label_owner]
        owners = {e["owner"] for e in candidates}
        if len(owners) != 1:
            continue
        quantities = [e for e in candidates if e["metric"] == "数量"]
        written_qty = re.search(r"(\d+)\s*(根|个|件|台|套|组)", label)
        if written_qty and len(quantities) == 1 and written_qty.group(2) == quantities[0]["unit"] and written_qty.group(1) != quantities[0]["value"]:
            issues.append("%s %s 的%s数量与原表不符：原PDF第%s页为%s%s" % (active[0], active[1], label_owner, quantities[0]["page"], quantities[0]["value"], quantities[0]["unit"]))
        for index, header in enumerate(headers):
            if index >= len(cells) or index == 0:
                continue
            metric = "缸径" if "缸径" in header else "行程" if "行程" in header else ""
            source = [e for e in candidates if metric and metric in e["metric"]]
            if len(source) != 1:
                continue
            actual = _NUMBER.findall(cells[index])
            expected = _NUMBER.findall(source[0]["value"])
            if metric == "缸径" and "柱径" in header and "柱径" not in source[0]["metric"]:
                rod = [e for e in candidates if e["metric"] == "柱径"]
                if len(rod) == 1:
                    expected += _NUMBER.findall(rod[0]["value"])
            if actual and expected and actual != expected:
                issues.append("%s %s 的%s%s与原表不符：原PDF第%s页为%s%s" % (active[0], active[1], label_owner, metric, source[0]["page"], source[0]["value"], source[0]["unit"]))
        for metric, pattern in (() if headers else (("行程", r"行程[为:：]?([\d.]+)"), ("缸径", r"[φΦØ]([\d.]+(?:/[φΦØ]?[\d.]+)+)"))):
            found = re.search(pattern, response)
            if not found:
                continue
            source = [e for e in candidates if metric in e["metric"]]
            if len(source) != 1:
                continue
            actual = _NUMBER.findall(found.group(1))
            expected = _NUMBER.findall(source[0]["value"])
            if actual and expected and actual != expected:
                issues.append("%s %s 的%s%s与原表不符：原PDF第%s页为%s%s" % (active[0], active[1], next(iter(owners)), metric, source[0]["page"], source[0]["value"], source[0]["unit"]))
    check_component_coverage()
    return list(dict.fromkeys(issues))

_LOT = re.compile(r"第\s*([一二三四五六七八九十0-9]+)\s*(?:标段|包)")
_LOT_BLOCK = re.compile(r"(?m)^\s*\d{2,4}\s+第\s*([一二三四五六七八九十0-9]+)\s*(?:标段|包)\s*[:：]")
_SHARED_AFTER_LOT = re.compile(r"(?m)^\s*(?:具体内容详见|交\s*货\s*(?:地点|期)|[一二三四五六七八九十]+、)")


def scope_numbered_lot_announcement(source: str, cover: str, project_name: str = "") -> tuple[str, str]:
    """仅当封面明确唯一标段且公告边界明确时，过滤其他编号标段。"""
    cover_lots = set(_LOT.findall(cover or ""))
    name_lots = set(_LOT.findall(project_name or ""))
    if len(cover_lots) != 1 or len(name_lots) > 1 or (name_lots and name_lots != cover_lots):
        return source, ""
    selected = next(iter(cover_lots))
    matches = list(_LOT_BLOCK.finditer(source or ""))
    if len(matches) < 2 or selected not in {m.group(1) for m in matches}:
        return source, ""
    # Never remove a final block unless a shared clause is explicitly found.
    end = _SHARED_AFTER_LOT.search(source, matches[-1].end())
    if not end:
        return source, ""
    if len(set(m.group(1) for m in matches)) != len(matches):
        return source, ""
    parts = [source[:matches[0].start()]]
    for i, match in enumerate(matches):
        stop = matches[i + 1].start() if i + 1 < len(matches) else end.start()
        if match.group(1) == selected:
            parts.append(source[match.start():stop])
    parts.append(source[end.start():])
    return "".join(parts), selected
