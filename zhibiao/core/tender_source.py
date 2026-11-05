"""招标原件的**字节**来源。

现状（2026-02 实测，`backend/app/pipeline_api.py`）：上传接口只把**文本**落库 ——

    upload_file() -> extract_text(filename, raw, ocr_cfg) -> insert into project_files(text, parse_method)

`raw` 解出来用完就丢，`project_files` 表里没有任何 bytea/blob 列，也没有落盘目录
（`deploy/docker-compose.yml` 只有 pgdata / valkeydata 两个卷；图片走的是
`project_images.bytes`，那是另一条路）。所以**今天拿不到原 PDF 字节**。

因此本模块的实现是「先按未来列名探一次，探不到就明确返回 None」：

1. 若 `project_files` 已经有字节列（阶段 3 起是 `raw bytea`，也兼容运维手工加过的
   `bytes`/`content`/`pdf_bytes`），直接取出来用；
2. 否则返回 None —— 调用方必须降级（走文本启发式），**绝不能让生成失败**。

阶段 3 起上传接口会用 `original_bytes_to_keep()` 决定要不要留原件：只留**真正的 PDF**
且不超过 30MB，docx/图片/超大文件一律不留（各有自己的解析路径，没必要撑大数据库）。

字节是**只读**的：只有"结构化还原表格"会读它，文本抽取用上传时的内存字节，
导出、体检、解读接口一律不碰（`select` 全部显式列名，见 tests/test_tender_bytes.py）。

查询口径与 `pipeline_api.tender_source_row` 完全一致（显式选择 > 解读锁定 >
最新上传），否则会出现"结构化用的是 A 文件、正文用的是 B 文件"的串档。
"""
from __future__ import annotations

from typing import Any

# 阶段 3 落库时用的列名优先；其余是历史/运维手工加过的可能性，按顺序试探。
_BYTE_COLUMNS = ("raw", "pdf_bytes", "content", "bytes")

# 留原件的上限：一份 80 页带图标的招标文件约 1~3MB，30MB 足够；再大就不是正常标书，
# 存进库只会把备份与迁移撑爆。超过就不留（上传照常成功，只是以后走降级路径）。
MAX_KEPT_BYTES = 30 * 1024 * 1024

# 与 pipeline_api.tender_source_row 同一套优先级，只多取 id 与 ext
_PICK_SQL = ("select f.id, f.filename, f.ext from project_files f "
             "join projects p on p.id=f.project_id "
             "left join project_interpret i on i.project_id=f.project_id "
             "where f.project_id::text=$1 order by "
             "(f.id=p.tender_file_id) desc nulls last, "
             "(f.id::text=coalesce(i.score_meta->>'file_id','')) desc, "
             "f.created_at desc, f.id desc limit 1")


def looks_like_pdf(blob: bytes | None) -> bool:
    """真 PDF 看文件头，不看后缀：`.pdf` 里装 docx/图片的情形真出现过。"""
    data = bytes(blob or b"")
    return data[:5] == b"%PDF-" or data[:1024].find(b"%PDF-") >= 0


def original_bytes_to_keep(filename: Any, raw: bytes) -> bytes | None:
    """上传时决定要不要把原件一起存进 `project_files.raw`。

    返回 None = **不留**，上传照常成功，只是这一份以后走文本启发式（degraded）。

    三条硬条件：
      1. 后缀是 `.pdf`（docx/xlsx/图片各有解析路径，不留原件）；
      2. 文件头真的是 PDF（不能只信后缀）；
      3. ≤ 30MB（`MAX_KEPT_BYTES`）。
    """
    blob = bytes(raw or b"")
    if not blob or len(blob) > MAX_KEPT_BYTES:
        return None
    if not str(filename or "").strip().lower().endswith(".pdf"):
        return None
    return blob if looks_like_pdf(blob) else None


def _is_pdf(filename: Any, ext: Any) -> bool:
    return str(ext or "").strip().lower() in ("pdf",) or str(filename or "").lower().endswith(".pdf")


async def tender_pdf_bytes(conn: Any, project_id: str, *,
                           file_id: str | None = None) -> bytes | None:
    """取当前项目招标原件的 PDF 字节；取不到返回 None。

    **不抛异常**：任何数据库/列不存在/文件类型不对/内容为空，一律 None。
    调用方（`format_forms`）拿到 None 就走原有的文本启发式并标 `degraded`。
    """
    if conn is None or not str(project_id or "").strip():
        return None
    try:
        if file_id:
            row = await conn.fetchrow(
                "select f.id, f.filename, f.ext from project_files f "
                "where f.id::text=$1 and f.project_id::text=$2", str(file_id), project_id)
        else:
            row = await conn.fetchrow(_PICK_SQL, project_id)
    except Exception:  # noqa: BLE001 - 查不到就是没有，不能把生成拖挂
        return None
    if not row or not _is_pdf(row["filename"], row["ext"]):
        return None

    for column in _BYTE_COLUMNS:
        try:
            data = await conn.fetchval(
                f"select {column} from project_files where id=$1", row["id"])  # noqa: S608 - 列名来自白名单常量
        except Exception:  # noqa: BLE001 - 该列不存在（UndefinedColumn）就试下一个
            continue
        if not data:
            continue
        blob = bytes(data)
        if looks_like_pdf(blob):
            return blob
        return None                   # 有字节但不是 PDF：宁可降级，不要瞎解析
    return None


async def source_original_bytes(conn: Any, project_id: str, file_id: str | None = None) -> bytes | None:
    """取招标原件的字节，**不限文件类型**（只要当时留下了原件）。

    与上面的 PDF 专用函数分开：按版式出稿要看原件，但对 docx 我们只在上传时切片、
    不留原件，所以这里返回 None 是正常情况，调用方按"没有原件"处理即可。
    列名仍然只从 `_BYTE_COLUMNS` 白名单里试，不直接写列名（`raw` 列的读取收敛在本模块）。
    """
    if conn is None:
        return None
    try:
        if file_id:
            row = await conn.fetchrow(
                "select id from project_files where id::text=$1 and project_id::text=$2",
                str(file_id), str(project_id))
        else:
            row = await conn.fetchrow(_PICK_SQL, str(project_id))
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    for column in _BYTE_COLUMNS:
        try:
            data = await conn.fetchval(
                f"select {column} from project_files where id=$1", row["id"])  # noqa: S608 - 列名来自白名单常量
        except Exception:  # noqa: BLE001
            continue
        if data:
            return bytes(data)
    return None
