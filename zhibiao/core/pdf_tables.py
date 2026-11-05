"""PDF 版式级表格提取：把表格从「按行拼接的字符串」提升成「行列网格 + 单元格 + 角色」。

为什么必须单独有一层（背景，见 docs/format-table-rebuild.md）：

    线上链路是 `pypdf` 的 `page.extract_text()` 再把页面 `"\\n".join`
    （`backend/app/pipeline_api.py:705-706`）。`extract_text()` 返回 `str`：
    列边界、单元格归属、合并关系在这一行之后**全部不可恢复**。下游
    `format_forms` 只能在字符串上按「两个空格」猜列（`format_forms.py:38`），
    上限就是猜列。实测同一张表（《新1.pdf》p62 的 7.1）：

        PDF 里          = 8 行 × 8 列的有框线网格
        纯文本里        = 5 行数据各只剩一个孤零零的 `1`/`2`/`3`/`4`/`5`

    本模块用 pdfplumber 从**线条与坐标**拿回网格，输出结构化模型给渲染层，
    渲染层不需要认识任何一张具体的表。

设计原则：

1. **只认几何与形状，不认具体文档**：列定义永远来自原文表头行；代码里只固化
   `header / data / total / note / signature / unit / placeholder` 这几种**角色**
   的判定规则，不固化任何列名。
2. **不确定就报错，不猜**：无文本层、探测到表但网格全空、解析异常，一律
   `ok=False` + `reason`，绝不返回一张"看着像表"的空表冒充成功。
3. **纯函数、不做 IO**：入参是 PDF 字节，出参是 dict；落库与调用由上层负责。
4. **可离线测试**：`table_model()` 只吃原始几何数据（bbox + 每行 cells + 文本网格），
   测试用 JSON fixture 就能跑，不需要把整份招标文件放进仓库。

返回形状：

    {
      "ok": bool,
      "reason": str,                # ok=False 的原因；ok=True 时为空或说明性文字
      "page_count": int,
      "has_text_layer": bool,
      "pages_with_tables": [int],   # 1-based
      "tables": [
        {"page": int, "bbox": [x0, top, x1, bottom],
         "n_rows": int, "n_cols": int,
         "rows": [[str, ...], ...],             # 网格；合并单元格的文本只在左上格，其余为 ""
         "cells": [{"text", "row", "col", "rowspan", "colspan", "role"}, ...],
         "header_rows": int,
         "lines_before": [str, ...],            # 表 bbox 上方、纵向最近的连续文本行
         "lines_after":  [str, ...]}, ...],
      "errors": [{"page": int, "reason": str}],
    }
"""
from __future__ import annotations

import io
import re
from typing import Any, Iterable, Sequence

# ── 角色判定用的形状规则（不允许出现任何具体项目/设备/列名常量）────────────────
# 合计/小计/总计/总价/合价：这些词是**表格语汇**，不是本份招标文件的内容。
_TOTAL_RE = re.compile(r"^(?:合\s*计|小\s*计|总\s*计|共\s*计|合\s*价|总\s*价)")
# 表下注释。「说明」也算同类；「说明书」这种列名不算（后面要求冒号/空白/行尾）。
_NOTE_RE = re.compile(r"^(?:注(?:[：:\s]|[一二三四五六七八九十\d]|$)|说\s*明(?:[：:\s、]|$)"
                      r"|附\s*注|[（(]注[）)]|【注】)", re.M)
# 落款标签：整段就是签字/盖章栏，不是列名（「投标人名称」不算，「投标人：」算）。
# 这一条必须**收紧**：诊断里最典型的错法就是把表内列名「法定代表人」当成落款搬走，
# 所以只认「标签本身 + 冒号后的空白/盖章提示」这种落款形状，不做包含匹配。
_SIGN_LABEL_RE = re.compile(r"^(?:投\s*标\s*人|供\s*应\s*商|响\s*应\s*人|法定代表人|单位负责人"
                            r"|委托代理人|授权代表人?|授权代理人|单位公章|日期|签字|签章|盖章)"
                            r"(?:签字|签章|盖章|公章|代表)?$")
# 明确的盖章/签字提示：「投标人： （盖单位章）」「法定代表人（签字或签章）」
_SIGN_SEAL_RE = re.compile(r"[（(][^）)]{0,14}(?:盖章|签章|签字|公章)[^）)]{0,14}[）)]")
# 冒号后面只剩空白/下划线/盖章提示
_SIGN_VALUE_RE = re.compile(r"^[：:\s　_＿—\-－]*(?:[（(][^）)]{0,14}[）)])?\s*$")
_DATE_LINE_RE = re.compile(r"^年\s*月\s*日$")
# 结构边界：标题行（不是"注释里的 2、…"）。遇到它，表前/表后的连续文本就到此为止。
_HEADING_RE = re.compile(r"^(?:第[一二三四五六七八九十百零〇\d]{1,3}[章卷节]"
                         r"|[一二三四五六七八九十]{1,3}\s*[、.．]"
                         r"|[（(][一二三四五六七八九十]{1,3}[）)]"
                         r"|\d+(?:\.\d+)+|附表\s*\d|表\s*\d)")
# 整行只有一个 1~3 位数字：PDF 页脚页码，不是正文
_PAGE_NUMBER_RE = re.compile(r"^\d{1,3}$")
# 占位行/省略行：`……`、`...`、全角省略号——是表格里"还有若干行"的写法，不是空行。
_PLACEHOLDER_RE = re.compile(r"^(?:[…⋯.．·]{2,}|[—－-]{3,})$")
# 表头词库：只用来判断"这一行像不像一行列名"，不参与建表。
_HEADER_HINTS = (
    "序号", "名称", "规格", "型号", "数量", "单价", "总价", "合价", "单位", "备注",
    "内容", "项目", "金额", "数量", "产地", "厂家", "制造商", "品牌", "税率", "重量",
    "条款", "偏差", "标准", "参数", "响应", "偏离", "期限", "日期", "电话", "传真",
    "地址", "资质", "证书", "等级", "类型", "编号", "代码", "资本", "性质", "范围",
)
# 列名片段里不能出现的句读（括号不算：「单价（元）」是列名）
_COLUMN_TEXT_PUNCT = "。；，、！？：:;,.!?"
_MAX_COLUMN_CHARS = 14          # 超过这个长度的单元格不可能是列名
_MIN_TABLE_COLUMNS = 2          # 只有 1 列的"表"是签名框/分隔线，不当表格
_MAX_HEADER_ROWS = 3            # 表头最多折成 3 行
_EDGE_TOL = 1.0                 # pt：坐标聚类容差（PDF 里同一根线常有 ±0.3pt 抖动）
_MAX_CONTEXT_LINES = 8          # 表前后各最多取几行
_MIN_TEXT_CHARS_PER_PAGE = 50   # 低于这个密度视为无文本层（与 OCR 判定口径一致）


# ══════════════════════════════════════════════════════════════════════════
# 一、纯几何：字节 → 每页的表格网格
# ══════════════════════════════════════════════════════════════════════════

def extract_tables_from_pdf(raw: bytes, *, pages: Iterable[int] | None = None,
                            max_pages: int = 200,
                            with_page_texts: bool = False) -> dict[str, Any]:
    """PDF 字节 → 结构化表格（见模块 docstring 的返回形状）。

    `pages` 给 1-based 页码集合时只解析这些页（生产只关心「投标文件格式」那一段）；
    不给则扫全篇（最多 `max_pages` 页，防止超大附件把上传接口拖死）。

    `with_page_texts=True` 时额外返回 `page_texts: {页码: 该页纯文本}`，
    供"按节标题在页面文本里定位"使用（结构化渲染要知道某节落在哪几页）。
    默认 False：大多数调用方不需要逐页文本，没必要多算一遍。

    失败一律 `ok=False`，且**不抛业务异常**：
      - `empty_file`        入参为空
      - `no_pdfplumber`     服务器没装 pdfplumber（requirements.txt 已声明）
      - `parse_error`       PDF 打不开/加密/内容流损坏
      - `no_text_layer`     没有文字层（扫描件）：必须走 OCR，绝不能当表格解析
      - `empty_grid`        探测到表格，但网格全空，或只有 1 列（签名框/分隔线，成不了表）
      - `degenerate_grid`   解析这些表时抛了异常（内容流损坏等），一张可用的表都没拿到
      - `no_pages`          传入的页码全在文档范围外
      - `no_tables_detected` 这份 PDF 确实没有带框线的表格：**不是失败**，
                            调用方应回退文本启发式，且不必标 degraded
    """
    report: dict[str, Any] = {"ok": False, "reason": "", "page_count": 0,
                              "has_text_layer": False, "pages_with_tables": [],
                              "tables": [], "errors": [], "page_texts": {}}
    if not raw:
        report["reason"] = "empty_file"
        return report
    try:
        import pdfplumber  # 局部导入：模块被 import 时不背 pdfminer 的启动成本
    except ImportError:
        report["reason"] = "no_pdfplumber"
        return report

    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            _fill_report(report, pdf, pages=pages, max_pages=max_pages,
                         with_page_texts=with_page_texts)
    except Exception:  # noqa: BLE001 - 损坏/加密 PDF 一律降级，不能把上传打挂
        report["ok"] = False
        report["reason"] = report["reason"] or "parse_error"
        report["tables"] = []
    return report


def _fill_report(report: dict[str, Any], pdf: Any, *, pages: Iterable[int] | None,
                 max_pages: int, with_page_texts: bool = False) -> None:
    """把 pdfplumber 的 Pdf 对象填进 report（拆出来是为了可测）。"""
    page_count = len(pdf.pages)
    report["page_count"] = page_count
    wanted = _wanted_pages(pages, page_count, max_pages)
    if not wanted:
        report["reason"] = "no_pages"
        return

    if not _has_text_layer(pdf, wanted):
        report["reason"] = "no_text_layer"
        return
    report["has_text_layer"] = True

    empty_tables = 0
    for number in wanted:
        page = pdf.pages[number - 1]
        if with_page_texts:
            try:
                report["page_texts"][number] = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - 取不到某页文本不该毁掉整份文档
                report["page_texts"][number] = ""
        try:
            found = page.find_tables()
        except Exception:  # noqa: BLE001 - 单页失败不能毁掉整份文档
            report["errors"].append({"page": number, "reason": "find_tables_error"})
            continue
        if not found:
            continue
        try:
            lines = page.extract_text_lines()
        except Exception:  # noqa: BLE001
            lines = []
        boxes = [tuple(t.bbox) for t in found]
        page_height = float(getattr(page, "height", 842.0) or 842.0)
        for index, table in enumerate(found):
            try:
                model = table_model(table_bbox=tuple(table.bbox),
                                    row_cells=[list(row.cells) for row in table.rows],
                                    grid=table.extract(),
                                    lines=lines,
                                    page=number,
                                    page_height=page_height,
                                    sibling_boxes=[b for k, b in enumerate(boxes) if k != index])
            except Exception:  # noqa: BLE001
                report["errors"].append({"page": number, "reason": "table_extract_error"})
                continue
            if model is None:
                empty_tables += 1
                continue
            report["tables"].append(model)
            if number not in report["pages_with_tables"]:
                report["pages_with_tables"].append(number)

    report["pages_with_tables"].sort()
    if report["tables"]:
        report["ok"] = True
        return
    if empty_tables:
        report["reason"] = "empty_grid"
    elif report["errors"]:
        report["reason"] = "degenerate_grid"
    else:
        # 这份 PDF 确实没有带框线的表格：不是失败，只是"没有可用的结构化表"，
        # 调用方应回退到文本启发式（不要把这种情况标成 degraded）。
        report["ok"] = True
        report["reason"] = "no_tables_detected"


def _wanted_pages(pages: Iterable[int] | None, page_count: int, max_pages: int) -> list[int]:
    """要扫描的页码（1-based，去重、保序、裁剪到文档范围）。"""
    if pages is None:
        limit = min(page_count, max(0, max_pages))
        return list(range(1, limit + 1))
    out: list[int] = []
    for number in pages:
        try:
            value = int(number)
        except (TypeError, ValueError):
            continue
        if 1 <= value <= page_count and value not in out:
            out.append(value)
    return out


def _has_text_layer(pdf: Any, wanted: Sequence[int]) -> bool:
    """有没有文字层：只抽查前 5 页，避免为了判定把整篇解析一遍。"""
    sample = list(wanted)[:5]
    chars = 0
    for number in sample:
        page = pdf.pages[number - 1]
        try:
            chars += len(page.chars)
        except Exception:  # noqa: BLE001
            continue
    return chars >= _MIN_TEXT_CHARS_PER_PAGE * max(1, len(sample))


# ══════════════════════════════════════════════════════════════════════════
# 二、单张表 → 模型（纯函数：只吃几何与文本，不碰 pdfplumber）
# ══════════════════════════════════════════════════════════════════════════

def table_model(*, table_bbox: Sequence[float], row_cells: list[list[Any]],
                grid: list[list[str | None]], page: int,
                lines: Sequence[dict[str, Any]] = (),
                sibling_boxes: Sequence[Sequence[float]] = (),
                page_height: float = 842.0) -> dict[str, Any] | None:
    """一张表 → 结构化模型；不再是一张"表"时返回 None。

    `row_cells` 是 pdfplumber `Table.rows[i].cells`：长度与列数一致，
    被合并单元格覆盖的位置是 `None`（这是算 rowspan/colspan 的唯一可靠依据）。
    `grid` 是 `Table.extract()`：与 `row_cells` 逐格对齐，被覆盖的位置为 `None`。

    合并单元格的文本**只写在左上格**，其余格留空字符串 —— 否则渲染时会变成
    「合计」横跨的每一列都写一遍「合计」。
    """
    if len(table_bbox) != 4 or not row_cells:
        return None

    # 1) 由所有单元格的边缘聚类出真实的行边界/列边界。
    #    不能用 table.columns/rows 的 bbox：存在合并单元格时它们会互相重叠
    #    （实测 p60 的 columns[1] = 114.5~236.7、columns[2] = 181.8~236.7）。
    xs: list[float] = []
    ys: list[float] = []
    for row in row_cells:
        for cell in row:
            if cell is None:
                continue
            xs.extend((float(cell[0]), float(cell[2])))
            ys.extend((float(cell[1]), float(cell[3])))
    if len(xs) < 4 or len(ys) < 4:
        return None
    col_edges = _cluster(xs)
    row_edges = _cluster(ys)
    n_cols = len(col_edges) - 1
    n_rows = len(row_edges) - 1
    if n_rows < 1 or n_cols < _MIN_TABLE_COLUMNS:
        return None

    # 2) 网格 + 单元格（合并信息只留在 cells 里，网格里被覆盖的格是 ""）
    rows: list[list[str]] = [[""] * n_cols for _ in range(n_rows)]
    cells: list[dict[str, Any]] = []
    for row_index, row in enumerate(row_cells):
        for col_index, cell in enumerate(row):
            if cell is None:
                continue
            text = _cell_text(grid, row_index, col_index)
            top = _index_of(row_edges, float(cell[1]))
            left = _index_of(col_edges, float(cell[0]))
            bottom = _index_of(row_edges, float(cell[3]))
            right = _index_of(col_edges, float(cell[2]))
            rowspan = max(1, bottom - top)
            colspan = max(1, right - left)
            if 0 <= top < n_rows and 0 <= left < n_cols:
                rows[top][left] = text
            cells.append({"text": text, "row": top, "col": left,
                          "rowspan": rowspan, "colspan": colspan, "role": "data"})

    if not any(text.strip() for row in rows for text in row):
        return None

    # 3) 表头行数与角色
    header_rows = _header_row_count(rows)
    _assign_roles(rows, cells, header_rows)
    before, after = _context_lines(lines, table_bbox, sibling_boxes, page_height)

    return {
        "page": page,
        "bbox": [round(float(value), 1) for value in table_bbox],
        "n_rows": n_rows,
        "n_cols": n_cols,
        "rows": rows,
        "cells": cells,
        "header_rows": header_rows,
        "lines_before": before,
        "lines_after": after,
    }


def _cluster(values: Sequence[float], tol: float = _EDGE_TOL) -> list[float]:
    """把带抖动的坐标聚成一条边（同一根线不同单元格可能差 0.x pt）。"""
    out: list[float] = []
    for value in sorted(float(v) for v in values):
        if not out or value - out[-1] > tol:
            out.append(value)
    return out


def _index_of(edges: Sequence[float], value: float) -> int:
    """这个坐标落在第几条网格线上（取最近的）。"""
    return min(range(len(edges)), key=lambda i: abs(edges[i] - value))


def _cell_text(grid: Sequence[Sequence[str | None]], row: int, col: int) -> str:
    """取单元格文本：去掉 PDF 折行（「型号/和规格」是「型号和规格」）。"""
    try:
        raw = grid[row][col]
    except (IndexError, TypeError):
        return ""
    if raw is None:
        return ""
    text = str(raw).replace("\r", "").replace("\n", "")
    return re.sub(r"[ \t　]{2,}", " ", text).strip()


def _is_column_like(text: str) -> bool:
    """这一格像不像列名：短、没有句读。"""
    clean = text.strip()
    if not clean or len(clean) > _MAX_COLUMN_CHARS:
        return False
    return not any(ch in clean for ch in _COLUMN_TEXT_PUNCT)


def _header_row_count(rows: Sequence[Sequence[str]]) -> int:
    """表头占前几行（0 表示这是一张"竖排标签/值"表，没有列名行）。

    判据全部是**结构性的**：行内被填的格子够多、都是短词、至少命中两个表头词。
    折行的表头在 pdfplumber 里会被纵向合并成**同一网格行**（实测 p62：型号/和规格
    这类折行是 rowspan 单元格，不是第二行），所以这里只处理"真的排成了两行"的情况
    （同一行里出现两组短列名），不把「1 2 3（1×2） 4」这类单位行并进表头 ——
    那是原文里真实存在的一行，并进表头等于把它从表体里删掉。
    """
    if not rows:
        return 0
    filled_counts = [sum(1 for value in row if value.strip()) for row in rows]
    widest = max(filled_counts) if filled_counts else 0
    if widest < 3:
        return 0                      # 竖排表（资格基本情况表）：没有列名行
    header = 0
    for index in range(min(_MAX_HEADER_ROWS, len(rows))):
        filled = [value for value in rows[index] if value.strip()]
        if not filled:
            break
        if sum(1 for value in filled if _is_column_like(value)) < max(2, len(filled) // 2):
            break
        hinted = sum(1 for value in filled if any(h in value for h in _HEADER_HINTS))
        if index == 0:
            if hinted < 1:
                break
            header = 1
            continue
        if hinted < 2:
            break
        header = index + 1
    return header


def _assign_roles(rows: Sequence[Sequence[str]], cells: list[dict[str, Any]],
                  header_rows: int) -> None:
    """给每个单元格打角色。**行级角色套到该行的所有格**，渲染层才能整行处理。"""
    row_role: dict[int, str] = {}
    for index, row in enumerate(rows):
        if index < header_rows:
            row_role[index] = "header"
            continue
        filled = [value.strip() for value in row if value.strip()]
        if not filled:
            # 整行空：保留成一行空单元格（表格框线还在），角色仍算 data
            row_role[index] = "data"
            continue
        if any(_PLACEHOLDER_RE.fullmatch(value) for value in filled) and len(filled) <= 2:
            row_role[index] = "placeholder"
            continue
        if _is_total_row(row):
            row_role[index] = "total"
        elif _is_note_text(filled[0]):
            row_role[index] = "note"
        elif any(_SIGN_SEAL_RE.search(value) for value in filled):
            # 表内只有"明确的盖章提示"才算落款；单独一个「法定代表人」
            # 是列名/行标签，绝不能判成落款（那正是原来错位的成因之一）。
            row_role[index] = "signature"
        else:
            row_role[index] = "data"
    for cell in cells:
        cell["role"] = row_role.get(int(cell["row"]), "data")


def _is_total_row(row: Sequence[str]) -> bool:
    """合计行：首格是合计类词，或某个横跨多列的格子是合计类词。"""
    for value in row:
        text = value.strip()
        if text and _TOTAL_RE.match(text):
            return True
    return False


def _is_note_text(text: str) -> bool:
    return bool(_NOTE_RE.match(str(text or "").strip()))


def _is_signature_text(text: str) -> bool:
    """落款形状。**标签必须"整段就是"签字/盖章栏**，不能是包含匹配。

    反例（必须判 False）：`投标人名称`、`法定代表人/（单位负责人）`、`签字人职务`
    —— 它们是表内列名/行标签；把这类文本判成落款，就会把表里的字搬出表外。
    """
    clean = str(text or "").strip()
    if not clean or len(clean) > 40:
        return False
    if _is_note_text(clean):
        return False
    if _DATE_LINE_RE.match(clean):
        return True
    if _SIGN_SEAL_RE.search(clean):
        return True
    label, sep, value = clean, "", ""
    for mark in ("：", ":"):
        if mark in clean:
            label, _, value = clean.partition(mark)
            sep = mark
            break
    label_clean = re.sub(r"\s+", "", label)
    if not _SIGN_LABEL_RE.match(label_clean):
        return False
    return bool(_SIGN_VALUE_RE.match(value if sep else ""))


def classify_line_role(text: str) -> str:
    """表外文本行的角色（供渲染层用）：note / signature / placeholder / body。"""
    clean = str(text or "").strip()
    if not clean:
        return "body"
    if _is_note_text(clean):
        return "note"
    if _PLACEHOLDER_RE.fullmatch(clean):
        return "placeholder"
    if _is_signature_text(clean):
        return "signature"
    return "body"


# ══════════════════════════════════════════════════════════════════════════
# 三、表格前后的注释/落款归属（修"注释落款跑位"的根）
# ══════════════════════════════════════════════════════════════════════════

def _context_lines(lines: Sequence[dict[str, Any]], table_bbox: Sequence[float],
                   sibling_boxes: Sequence[Sequence[float]],
                   page_height: float = 842.0) -> tuple[list[str], list[str]]:
    """表 bbox 上下方**紧贴这张表、直到结构边界为止**的连续文本行。

    为什么不用一条固定距离阈值：实测同一份 PDF 里，表下注释离表只有 18pt，
    而落款（投标人/法定代表人/年月日）离表 41~86pt（p60/p62/p64 均如此，PDF 给
    手写留了大片空白）。用距离阈值要么漏掉落款，要么把整版都卷进来。

    所以归属判据是**两道硬边界 + 一道距离上限**：
      1. 不越过同页上的另一张表（`sibling_boxes`）；
      2. 不越过标题行（`_HEADING_RE`：「7.1…」「（一）…」「一、…」「第N章」）——
         标题是结构边界，标题之后的文字不属于这张表；
      3. 整段距离不超过页高的 45%（再远就不是"这张表附近"了）；
      4. 整行只有 1~3 位数字的页脚页码直接跳过。
    这样（一）的「注：…」只可能归到（一）的表下，不会跨到（七）。
    """
    top = float(table_bbox[1])
    bottom = float(table_bbox[3])
    page_height = float(page_height or 842.0)
    far = page_height * 0.55        # 实测 p60 的「年 月 日」离表 392pt（页高 842），再小就漏
    usable = [line for line in lines if str(line.get("text") or "").strip()]

    def center(line: dict[str, Any]) -> float:
        return (float(line["top"]) + float(line["bottom"])) / 2.0

    def inside_table(line: dict[str, Any]) -> bool:
        return top - 1.0 <= center(line) <= bottom + 1.0

    def fence(edge: float, going_up: bool) -> float | None:
        """上下方最近的那张兄弟表，越过它就不算本表的上下文。"""
        best: float | None = None
        for box in sibling_boxes:
            if going_up and float(box[3]) <= edge:
                best = float(box[3]) if best is None else max(best, float(box[3]))
            if (not going_up) and float(box[1]) >= edge:
                best = float(box[1]) if best is None else min(best, float(box[1]))
        return best

    def keep(text: str) -> bool:
        """页码不是正文；标题是边界（由调用方提前 break）。"""
        clean = str(text or "").strip()
        return bool(clean) and not _PAGE_NUMBER_RE.match(clean)

    above = sorted([line for line in usable
                    if float(line["bottom"]) <= top + 1.0 and not inside_table(line)],
                   key=lambda line: -float(line["bottom"]))
    below = sorted([line for line in usable
                    if float(line["top"]) >= bottom - 1.0 and not inside_table(line)],
                   key=lambda line: float(line["top"]))

    fence_up = fence(top, going_up=True)
    before: list[str] = []
    for line in above:
        if fence_up is not None and float(line["top"]) < fence_up:
            break
        if top - float(line["top"]) > far:
            break
        if _is_heading(str(line["text"])):
            break
        if not keep(str(line["text"])):
            continue
        before.append(str(line["text"]).strip())
        if len(before) >= _MAX_CONTEXT_LINES:
            break

    fence_down = fence(bottom, going_up=False)
    after: list[str] = []
    for line in below:
        if fence_down is not None and float(line["bottom"]) > fence_down:
            break
        if float(line["bottom"]) - bottom > far:
            break
        if _is_heading(str(line["text"])):
            break
        if not keep(str(line["text"])):
            continue
        after.append(str(line["text"]).strip())
        if len(after) >= _MAX_CONTEXT_LINES:
            break

    return list(reversed(before)), after


def _is_heading(text: str) -> bool:
    """标题行（结构边界）。注释行即使带编号也不算标题。"""
    clean = str(text or "").strip()
    if not clean or len(clean) > 60:
        return False
    if _is_note_text(clean) or _PLACEHOLDER_RE.fullmatch(clean):
        return False
    return bool(_HEADING_RE.match(clean))