# -*- coding: utf-8 -*-
"""把招标文件「投标文件格式」章的版式原样接进成稿。

背景：招标文件常给出第六章「投标文件格式」（投标函 / 身份证明 / 授权委托书 / 偏差表 /
分项报价表 …），这些页面的版式由招标人规定，投出去的标书必须与之逐项一致（字体、字号、
表格合并、分页位置都在评审范围内）。平台原先的做法是把格式原文**转成文本再重排**，
文字对得上、版式全丢。这里改为：直接拿招标原件字节，把对应小节的 XML 原样搬进成稿。

设计取舍（都是为了让"没命中"时行为与历史完全一致）：

* 只做「有把握」的替换：`detect()` 判定不成立、或某个章节标题在模板里找不到对应小节，
  一律**沿用原有的文本渲染**，不抛错、不空章——导出是交付动作，不能因为一个启发式判定
  把整份文件搞坏；
* 解析放在导出时做，用库里已有的 `project_files.raw`（原件字节），因此不需要新表、
  不需要改上传流程、更不需要界面上多一个按钮；
* 同一份原件只解析一次（`_parse_cache`），避免 N 个章节各解析一遍 2 MB 的 docx。
"""

from __future__ import annotations

import io
import re
from typing import Any, Iterable, Mapping

import docx
from zhibiao.core.format_template import SLICE_VERSION

# 章节标题归一：去编号、去括号、去空白、全角转半角，只留可比较的实词。
_NUM_PREFIX = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节部分]|[一二三四五六七八九十百]{1,3}\s*[、.．)]|"
    r"[（(][一二三四五六七八九十百\d]{1,3}[）)]|\d{1,2}(?:\.\d{1,2})*\s*[、.．)]?)\s*"
)
_SPACE = re.compile(r"[\s\u3000]+")
_PUNCT = re.compile(r"[。，、；：.,;:（）()【】\[\]“”\"'’‘]")

# 只有这些「容器章」才可能整章照模板落——避免把正文技术章节误当成格式模板。
_CONTAINER_HINTS = ("投标文件格式", "响应文件格式", "投标文件组成", "投标文件的编制")

# 一次导出内复用解析结果：键为 (sha1 前 16 位, 扩展名)。
_parse_cache: dict[tuple[str, str], Any] = {}

# 缓存上限：8 份招标文件。超出后按插入顺序丢最旧的（够用且不会无限增长）。
_CACHE_LIMIT = 8


def normalize_title(text: str) -> str:
    """标题归一，用于模板小节与成稿章节的匹配（不用于展示）。"""
    value = str(text or "").strip()
    value = _SPACE.sub("", value)
    value = _NUM_PREFIX.sub("", value)
    value = _PUNCT.sub("", value)
    return value


def _cache_key(raw: bytes, filename: str) -> tuple[str, str]:
    import hashlib

    ext = str(filename or "").rsplit(".", 1)[-1].lower() if "." in str(filename or "") else ""
    return hashlib.sha1(raw[:2_000_000] + str(len(raw)).encode()).hexdigest()[:16], ext


def _engine_for(raw: bytes, filename: str):
    """按扩展名选引擎；docx 优先（版式可 1:1 保留），其余回落 PDF 引擎。"""
    ext = str(filename or "").rsplit(".", 1)[-1].lower() if "." in str(filename or "") else ""
    try:
        if ext in ("docx", "doc", "wps"):
            from zhibiao.core import format_template

            return format_template, "docx"
        if ext == "pdf":
            from zhibiao.core import pdf_format_template

            return pdf_format_template, "pdf"
    except Exception:  # noqa: BLE001 —— 引擎缺失只意味着「这次不按模板出」，不能影响导出
        return None, ""
    return None, ""


def build_templates(raw: bytes, filename: str,
                    rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """给每个能对上模板小节的章节返回它的「版式切片」。

    返回 `{章节id: slice}`；`slice` 的类型由引擎决定（docx 引擎给 docx 字节）。
    返回空字典表示本次不按模板出稿（沿用历史文本渲染）。
    """
    if not raw:
        return {}
    engine, kind = _engine_for(bytes(raw), filename)
    if engine is None:
        return {}
    key = _cache_key(bytes(raw), filename)
    try:
        if key not in _parse_cache:
            info = engine.detect(bytes(raw), filename)
            if not info.get("has_template"):
                _parse_cache[key] = {"info": info, "sections": {}, "kind": kind}
            else:
                sections: dict[str, Any] = {}
                # 用 extract_sections 一次拿全，而不是把 detect 的标题再逐条去切：
                # 真实招标文件的第六章开头常有一份「小节目录」（标题重复出现），
                # 引擎在那里已经做过"取正文那一处"的判定，直接复用它的结果更可靠。
                items = engine.extract_sections(bytes(raw), filename) or []
                for item in items:
                    title = str(item.get("title") or "").strip()
                    # match_key 由引擎给出的归一化标题优先（可能带适用条件后缀，避免撞名）
                    section_key = str(item.get("match_key") or "").strip() or normalize_title(title)
                    if not section_key:
                        continue
                    # 归一化后撞名的（「七、分项报价表」与「（二）分项报价表」）必须分开存：
                    # 挤在同一个键上会一个覆盖一个，成稿里就少一整张表。
                    if section_key in sections:
                        suffix = 2
                        while "%s·%d" % (section_key, suffix) in sections:
                            suffix += 1
                        section_key = "%s·%d" % (section_key, suffix)
                    blob = None
                    if kind == "docx":
                        try:
                            blob = _slice_one(engine, bytes(raw), title, item.get("index"))
                        except Exception:  # noqa: BLE001 —— 单个小节失败不影响其它小节
                            blob = None
                    if blob:
                        sections[section_key] = (title, blob)
                _parse_cache[key] = {"info": info, "sections": sections, "kind": kind}
                # 缓存必须有上限：每个键存的是若干个小节的 docx 字节（几十上百 KB），
                # 服务器进程长期运行、项目又不断切换，无界缓存会慢慢把内存吃掉。
                while len(_parse_cache) > _CACHE_LIMIT:
                    _parse_cache.pop(next(iter(_parse_cache)), None)
        parsed = _parse_cache[key]
    except Exception:  # noqa: BLE001
        return {}

    sections: dict[str, Any] = parsed.get("sections") or {}
    if not sections:
        return {}

    out: dict[str, Any] = {}
    for row in rows or []:
        title = normalize_title(str(row.get("title") or ""))
        if not title:
            continue
        hit = sections.get(title)
        if hit is None:
            # 允许「章标题 = 容器章」时整章套用（少数项目把格式章建成一个平章）
            for name, value in sections.items():
                if name and (name in title or title in name) and len(min(name, title, key=len)) >= 4:
                    hit = value
                    break
        if hit is None:
            continue
        _title, blob = hit
        if blob:
            out[str(row.get("id") or "")] = blob
    return out


def describe(rows: list[Mapping[str, Any]], templates: Mapping[str, Any]) -> list[dict[str, Any]]:
    """导出提示用：哪些章节是按模板版式落的（中性描述，不做合规判断）。"""
    notes: list[dict[str, Any]] = []
    if not templates:
        return notes
    for row in rows or []:
        if str(row.get("id") or "") not in templates:
            continue
        notes.append({
            "seq": int(row.get("seq") or 0),
            "title": str(row.get("title") or ""),
            "kind": "版式",
            "detail": "按招标文件格式原样出稿",
        })
    return notes


# ───────────────────────── 切片持久化 ─────────────────────────
# 为什么要在上传时存切片、而不是导出时再从原件解析：
#   仓库有一条既有约束（`backend/tests/test_tender_bytes.py`）：`project_files.raw` 只对
#   **真 PDF 且 ≤30MB** 留存，docx 原件根本不入库。也就是说 Word 招标文件在导出时早已
#   没有字节可用。切片本身只有几百 KB（12 小节），上传时顺手存下来，导出就只是读库+拼装：
#   既不改变"raw 不留 docx"的既有策略，也不用在导出请求里重新解析 2MB 的 docx。

_DDL = """
create table if not exists format_templates (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,
  file_id uuid not null,
  title text not null,
  match_key text not null,
  slice bytea not null,
  created_at timestamp with time zone not null default now(),
  unique (project_id, match_key)
)
"""

_ddl_ready = False


async def ensure_table(conn: Any) -> None:
    """建表只做一次；失败不抛（导出会因为没有切片自动退回文本渲染）。"""
    global _ddl_ready
    if _ddl_ready:
        return
    try:
        await conn.execute(_DDL)
        _ddl_ready = True
    except Exception:  # noqa: BLE001
        pass


def _slice_one(engine: Any, raw: bytes, title: str, index: Any = None) -> bytes | None:
    """切一个小节；引擎不支持按位次定位时退回按标题切（旧实现/测试替身都要能跑）。"""
    try:
        return engine.extract_section_docx(raw, title, index=index)
    except TypeError:
        return engine.extract_section_docx(raw, title)


def extract_slices(raw: bytes, filename: str) -> dict[str, tuple[str, bytes]]:
    """把招标原件切成 `{归一化标题: (原标题, 切片 docx)}`；不具备条件时返回空字典。"""
    engine, kind = _engine_for(bytes(raw or b""), filename)
    if engine is None or kind != "docx":
        return {}
    try:
        info = engine.detect(bytes(raw), filename)
        if not info.get("has_template"):
            return {}
        items = engine.extract_sections(bytes(raw), filename) or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, tuple[str, bytes]] = {}
    for item in items:
        title = str(item.get("title") or "").strip()
        key = str(item.get("match_key") or "").strip() or normalize_title(title)
        if not title or not key:
            continue
        # 归一化后撞名的（「七、分项报价表」与「（二）分项报价表」）必须分开存：
        # 挤在同一个键上会一个覆盖一个，成稿里就少一整张表。
        if key in out:
            suffix = 2
            while "%s·%d" % (key, suffix) in out:
                suffix += 1
            key = "%s·%d" % (key, suffix)
        try:
            blob = _slice_one(engine, bytes(raw), title, item.get("index"))
        except Exception:  # noqa: BLE001
            blob = None
        if blob:
            out[key] = (title, blob)
    return out


async def store_slices(conn: Any, project_id: str, file_id: str,
                       raw: bytes, filename: str) -> int:
    """上传时把切片写库；同一项目同一小节重复上传则覆盖（幂等）。"""
    import asyncio

    try:
        slices = await asyncio.to_thread(extract_slices, bytes(raw or b""), filename)
    except Exception:  # noqa: BLE001
        return 0
    if not slices:
        return 0
    await ensure_table(conn)
    for key, (title, blob) in slices.items():
        try:
            await conn.execute(
                "insert into format_templates (project_id, file_id, title, match_key, slice) "
                "values ($1,$2,$3,$4,$5) "
                "on conflict (project_id, match_key) do update set "
                "file_id=excluded.file_id, title=excluded.title, slice=excluded.slice, "
                "created_at=now()",
                project_id, str(file_id), title, key, blob)
        except Exception:  # noqa: BLE001 —— 存不下只影响版式佳化，不能让上传失败
            continue
    return len(slices)


async def load_slices(conn: Any, project_id: str, file_id: str | None = None) -> dict[str, tuple[str, bytes]]:
    """读回某项目的切片；给了 file_id 就只取那一份招标文件的。"""
    # 建表是「顺手补一次」，不是前置条件：导出请求拿到的连接可能处在只读/受限事务里，
    # 一条 create table 失败就会把下面的查询一起吞掉，整份标书就静默退回文本渲染
    # （症状：字体、边距、分页全变成平台的默认值）。表早就在，查不到再报错也不迟。
    try:
        await ensure_table(conn)
    except Exception:  # noqa: BLE001
        pass
    try:
        if file_id:
            rows = await conn.fetch(
                "select title, match_key, slice from format_templates "
                "where project_id::text=$1 and file_id::text=$2", str(project_id), str(file_id))
        else:
            rows = await conn.fetch(
                "select title, match_key, slice from format_templates "
                "where project_id::text=$1", str(project_id))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, tuple[str, bytes]] = {}
    for row in rows or []:
        blob = row["slice"]
        if blob:
            out[str(row["match_key"])] = (str(row["title"]), bytes(blob))
    return out


_SUB_NUMBER = re.compile(r"^\s*[（(][一二三四五六七八九十\d]{1,3}[）)]")


def _section_level(title: str) -> int:
    """按原件的编号形式定层级：「一、投标函」是一级，「（一）基本情况表」是二级。"""
    return 2 if _SUB_NUMBER.match(str(title or "")) else 1


async def template_texts(conn: Any, project_id: str, file_id: str | None = None) -> dict[str, str]:
    """按小节标题取每张切片的纯文字（表格按单元格拼成行）。

    投标函、身份证明、报价表这些章节是招标文件把格式钉死的表单，正文就该是原件的
    文字，不需要模型再写一遍；前端显示的字数也应当等于原件字数。
    """
    rows = await load_slices(conn, project_id, file_id)
    out: dict[str, str] = {}
    for key, (title, blob) in (rows or {}).items():
        try:
            out[str(title)] = _slice_text(blob)
        except Exception:  # noqa: BLE001 —— 取不到文字就当没有，不影响大纲
            continue
    return out


def _slice_text(blob: bytes) -> str:
    """docx 切片 → 纯文字（段落一行，表格一行一格）。"""
    import io as _io

    from docx import Document as _Document
    from docx.oxml.ns import qn as _qn

    # 取文字必须用引擎那套口径（`w:t` + `w:sym`）：老切片是把符号写法的正文原样存进
    # 来的，只读 `w:t` 会把「（9）投标设备技术性能指标的详细描述；」读成前半句。
    from zhibiao.core.format_template import element_text

    document = _Document(_io.BytesIO(blob))
    lines: list[str] = []
    for child in document.element.body:
        if child.tag == _qn("w:p"):
            text = element_text(child).strip()
            if text:
                lines.append(text)
        elif child.tag == _qn("w:tbl"):
            for tr in child.findall(_qn("w:tr")):
                cells = []
                for tc in tr.findall(_qn("w:tc")):
                    cells.append(element_text(tc).strip())
                line = "\t".join(cells).strip()
                if line:
                    lines.append(line)
    return "\n".join(lines)


async def sections_outline(conn: Any, project_id: str,
                           file_id: str | None = None) -> list[tuple[int, str]]:
    """按招标文件「投标文件格式」章的小节原样给出目录。

    招标文件规定了投标文件由哪些部分组成、每部分叫什么、谁在前谁在后，这就是这份标书的
    目录。目录必须是它，不能是模型另起一套章节名——那会和招标人要求的格式逐条对不上。
    取不到（没上传 / 没有格式章 / 老切片）就返回空列表，调用方沿用原来的目录生成逻辑。
    """
    await ensure_table(conn)
    rows: list[Any] = []
    try:
        if file_id:
            rows = await conn.fetch(
                "select title from format_templates where project_id::text=$1 and file_id::text=$2 "
                "order by created_at, title", str(project_id), str(file_id))
        else:
            rows = await conn.fetch(
                "select title from format_templates where project_id::text=$1 "
                "order by created_at, title", str(project_id))
    except Exception:  # noqa: BLE001
        rows = []
    titles = [str(r["title"] or "").strip() for r in rows or []]
    titles = [t for t in titles if t]
    if not titles:
        # PDF 招标文件没有切片，只有"小节 → 页码"
        try:
            await ensure_pdf_table(conn)
            pdf_rows = await conn.fetch(
                "select title from format_pdf_sections where project_id::text=$1 "
                "order by start_page, title", str(project_id))
            titles = [str(r["title"] or "").strip() for r in pdf_rows or []]
            # 投标人须知/评标办法里的流程条款不是投标文件章节。这份招标文件没有独立的
            # 「投标文件格式」章时，检测到的小节就是（四）投标、（五）开标、（六）评标、
            # （七）授予合同——照单全收会让大纲错成四个流程条款（实测 1023——pdf02）。
            # 只在这一条（PDF 小节）过滤：Word 切片在上面已经返回，不受影响。
            titles = [t for t in titles if t and not _FLOW_SECTION.search(t)]
        except Exception:  # noqa: BLE001
            titles = []
    seen: set[str] = set()
    out: list[tuple[int, str]] = []
    for title in titles:
        if title in seen:
            continue
        seen.add(title)
        out.append((_section_level(title), title))
    return out


# 评标流程/须知类小节，不是投标文件要写的章节
_FLOW_SECTION = re.compile(
    r"开标|评标|授予合同|定标|中标通知|签订合同|质疑|投诉|废标|重新招标|投标人须知")

def _best_key(title: str, keys: Iterable[str]) -> str | None:
    """在模板小节里找和章节标题最像的一个。

    招标文件的章标题和格式章里的小节标题常写法不同，比如章里写「二、法定代表人身份证明」，
    小节却写「二、法定代表人（单位负责人）身份证明」，去括号归一后也不是子串关系。
    所以：完全相等 > 互相包含 > 相似度（≥0.62 才认），都不过就当作没有模板——
    宁可让这一章用平台排版，也不要把别人的表格塞进来。
    """
    import difflib

    if not title:
        return None
    best: tuple[float, str] | None = None
    for key in keys:
        if not key:
            continue
        if abs(len(key) - len(title)) > max(6, int(len(title) * 0.6)):
            continue
        # 撞名小节带 `~2` 去重后缀（见 pdf_sections），比对前摘掉，否则会掉到 0.62 以下
        head, _, tail = key.rpartition("~")
        base = head if (head and tail.isdigit()) else key
        if base == title:
            return key
        if len(base) >= 4 and len(title) >= 4 and (base in title or title in base):
            score = 0.9
        else:
            score = difflib.SequenceMatcher(None, base, title).ratio()
        if score >= 0.85 and (best is None or score > best[0]):
            best = (score, key)
    return best[1] if best else None


def pdf_templates_for_rows(sections: Mapping[str, Any],
                           rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """把 PDF 小节按章节标题对上号：`{章节id: 小节信息}`（值原样透传）。

    匹配分两级，因为归一化会**抹掉编号**：「七、分项报价表」与「（二）分项报价表」
    归一后是同一个词（只靠 `~2` 后缀区分）。只按相似度比，后一个章节会抢到前一个
    小节的页区间——实测成稿里那 6 页分项报价表整段消失，目录还多一条重名条目：

      1. 原标题逐字相等（只去首尾空白）：「（二）分项报价表」对上它自己那一节；
      2. `_best_key` 相似度（≥0.62），比对前摘掉 `~N` 后缀。

    一个小节只能配一次：已配过的键不再参与后面的匹配，避免两章指向同一份内容。

    两章都在时各拿各的页；只有章标题那一章在时，把它名下同基名的子节页也并进来
    （否则「七、分项报价表」只拿到标题页，后面 6 页表格整段丢失——内容缺失）。
    """
    if not sections:
        return {}

    def _base(key: str) -> str:
        head, _, tail = str(key).rpartition("~")
        return head if (head and tail.isdigit()) else str(key)

    groups: dict[str, list[tuple[str, int, int]]] = {}
    for key, value in (sections or {}).items():
        groups.setdefault(_base(key), []).append(value)

    exact: dict[str, str] = {}
    for key, value in (sections or {}).items():
        name = value[0] if isinstance(value, (tuple, list)) and value else ""
        compact = str(name or "").strip()
        if compact:
            exact.setdefault(compact, key)

    matched: dict[str, str] = {}          # 章节id -> 小节键
    used: set[str] = set()
    for row in rows or []:
        key = exact.get(str(row.get("title") or "").strip())
        if key is None or key in used:
            continue
        matched[str(row.get("id") or "")] = key
        used.add(key)
    for row in rows or []:
        row_id = str(row.get("id") or "")
        if row_id in matched:
            continue
        title = normalize_title(str(row.get("title") or ""))
        if not title:
            continue
        key = _best_key(title, [k for k in sections.keys() if k not in used])
        if key is None:
            continue
        matched[row_id] = key
        used.add(key)

    # 一个基名被几个章节认领了：>1 说明这些章节要各自展示自己那几页（不能合并，
    # 合并会把同一批页在成稿里印两遍）；==1 才把同基名的兄弟小节并进来补全。
    owners: dict[str, int] = {}
    for key in matched.values():
        owners[_base(key)] = owners.get(_base(key), 0) + 1

    out: dict[str, Any] = {}
    for row_id, key in matched.items():
        base = _base(key)
        group = groups.get(base) or [sections[key]]
        if owners.get(base, 0) > 1 or len(group) == 1:
            out[row_id] = sections[key]
        else:
            out[row_id] = (group[0][0], min(g[1] for g in group), max(g[2] for g in group))
    return out


# PDF 小节的页码落库：识别一次 100 多页要 20 秒，导出时不该重扫。
_PDF_MAX_BYTES = 12 * 1024 * 1024   # 超过 12MB 的标书不猜版式（逐页读文字层太慢）
_PDF_MAX_PAGES = 320                # 超过 320 页同理

_PDF_VERSION = "zbt-pdf-2"   # 页区间算法改了就升这个版本，旧行自动作废重算

_PDF_DDL = """
create table if not exists format_pdf_sections (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,
  file_id uuid not null,
  title text not null,
  match_key text not null,
  start_page integer not null,
  end_page integer not null,
  version text not null default '',
  created_at timestamp with time zone not null default now(),
  unique (project_id, match_key)
)
"""

_pdf_ddl_ready = False


async def ensure_pdf_table(conn: Any) -> None:
    global _pdf_ddl_ready
    if _pdf_ddl_ready:
        return
    try:
        await conn.execute(_PDF_DDL)
        # 老库：表已存在但没有 version 列。加上，旧行取默认空串 -> 下面按版本过滤时作废。
        await conn.execute(
            "alter table format_pdf_sections "
            "add column if not exists version text not null default ''")
        _pdf_ddl_ready = True
    except Exception:  # noqa: BLE001
        pass


def pdf_sections(raw: bytes, filename: str = "") -> dict[str, tuple[str, int, int]]:
    """`{归一化标题: (原标题, 起始页0基, 结束页0基)}`；不是 PDF/没有格式章返回空。

    识别要逐页读文字层（100 页约 20 秒），所以设了上限：超大的标书不去猜版式，
    直接回落平台排版——宁可慢一点也不要让上传卡死。
    """
    if not raw or len(raw) > _PDF_MAX_BYTES:
        return {}
    try:
        from zhibiao.core import pdf_format_template

        if pdf_format_template.page_count(bytes(raw)) > _PDF_MAX_PAGES:
            return {}
        info = pdf_format_template.detect(bytes(raw), filename)
        if not info.get("has_template"):
            return {}
        out: dict[str, tuple[str, int, int]] = {}
        chapter_start = max(0, int(info.get("start_page") or 1) - 1)
        for position, item in enumerate(pdf_format_template.extract_sections(bytes(raw), filename) or []):
            title = str(item.get("title") or "").strip()
            key = str(item.get("match_key") or "").strip() or normalize_title(title)
            if not title or not key:
                continue
            first = int(item.get("start_page") or 1) - 1
            # 第一节从「第六章 投标文件格式」那一页起算：章封面、章目录都属于格式章，
            # 少了它们，成稿里就没有招标文件那一页“投标文件格式”的目录。
            if position == 0:
                first = min(first, chapter_start)
            # 不同层级的小节可能归一成同一个键（「七、分项报价表」与「（二）分项报价表」），
            # 直接 out[key]= 会让后一个覆盖前一个，成稿目录里就凭空少一节。
            # 加 `~2` 这种去重后缀保命；_best_key 比对时会把这层后缀摘掉。
            unique = key
            serial = 2
            while unique in out:
                unique = f"{key}~{serial}"
                serial += 1
            out[unique] = (title, first, int(item.get("end_page") or 1) - 1)
        return out
    except Exception:  # noqa: BLE001
        return {}


async def store_pdf_sections(conn: Any, project_id: str, file_id: str,
                             raw: bytes, filename: str) -> int:
    """上传时把 PDF 小节的页码存好（幂等覆盖），导出就不再重扫文字层。"""
    import asyncio

    try:
        found = await asyncio.to_thread(pdf_sections, bytes(raw or b""), filename)
    except Exception:  # noqa: BLE001
        return 0
    if not found:
        return 0
    await ensure_pdf_table(conn)
    for key, (title, first, last) in found.items():
        if _FLOW_SECTION.search(str(title or "")):
            continue        # 开标/评标/授予合同是流程条款，不是投标文件格式页
        try:
            await conn.execute(
                "insert into format_pdf_sections "
                "(project_id, file_id, title, match_key, start_page, end_page, version) "
                "values ($1,$2,$3,$4,$5,$6,$7) on conflict (project_id, match_key) do update set "
                "file_id=excluded.file_id, title=excluded.title, "
                "start_page=excluded.start_page, end_page=excluded.end_page, "
                "version=excluded.version, created_at=now()",
                project_id, str(file_id), title, key, first, last, _PDF_VERSION)
        except Exception:  # noqa: BLE001
            continue
    return len(found)


async def load_pdf_sections(conn: Any, project_id: str,
                            file_id: str | None = None) -> dict[str, tuple[str, int, int]]:
    """读回 PDF 小节页码：`{归一化标题: (标题, 起始页, 结束页)}`。"""
    await ensure_pdf_table(conn)
    try:
        if file_id:
            rows = await conn.fetch(
                "select title, match_key, start_page, end_page from format_pdf_sections "
                "where project_id::text=$1 and file_id::text=$2 and version=$3",
                str(project_id), str(file_id), _PDF_VERSION)
        else:
            rows = await conn.fetch(
                "select title, match_key, start_page, end_page from format_pdf_sections "
                "where project_id::text=$1 and version=$2", str(project_id), _PDF_VERSION)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, tuple[str, int, int]] = {}
    for row in rows or []:
        if _FLOW_SECTION.search(str(row["title"] or "")):
            continue        # 老数据里遗留的流程条款，同样不参与匹配
        out[str(row["match_key"])] = (str(row["title"]), int(row["start_page"]), int(row["end_page"]))
    return out


def _slice_is_current(blob: bytes) -> bool:
    """切片是不是"带样式表"的这一版产出的。

    老切片里没有原件的样式表，段落的 `w:pStyle` 是空引用，出稿会缩进、行距、
    表格边框全部退回默认——"内容对、位置全错"，比回落到文本排版更糟。所以宁可不认。
    """
    try:
        document = docx.Document(io.BytesIO(bytes(blob)))
    except Exception:  # noqa: BLE001 —— 连 docx 都不是：交给下游自己回落，不在这里拦
        return True
    try:
        return str(document.core_properties.comments or "") == SLICE_VERSION
    except Exception:  # noqa: BLE001
        return False


def templates_for_rows(slices: Mapping[str, tuple[str, bytes]],
                       rows: list[Mapping[str, Any]]) -> dict[str, bytes]:
    """把切片按章节标题对上号，返回 `{章节id: 切片docx}`（对不上就跳过，走历史渲染）。"""
    if not slices:
        return {}
    # 只拿第一条探一次版本：全库切片是同一版代码同一次上传写的
    first = next(iter(slices.values()))[1]
    if first and not _slice_is_current(first):
        return {}
    out: dict[str, bytes] = {}
    used: set[str] = set()
    # 先按原标题逐字相等对一遍：「七、分项报价表」和「（二）分项报价表」归一化之后是
    # 同一个词，只靠相似度会让两张不同的表互相抢位，成稿里就少一整张表。
    exact = {str(name or "").strip(): key for key, (name, _b) in (slices or {}).items()}
    for row in rows or []:
        key = exact.get(str(row.get("title") or "").strip())
        if key is None or key in used:
            continue
        blob = slices[key][1]
        if blob:
            out[str(row.get("id") or "")] = blob
            used.add(key)
    for row in rows or []:
        if str(row.get("id") or "") in out:
            continue
        title = normalize_title(str(row.get("title") or ""))
        if not title:
            continue
        key = _best_key(title, [k for k in slices.keys() if k not in used])
        if key is None:
            continue
        blob = slices[key][1]
        if blob:
            out[str(row.get("id") or "")] = blob
            used.add(key)
    return out