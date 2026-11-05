"""Persist only Word cover artifacts while upload still has the original layout.

This is separate from chapter slices and from the PDF-only raw-byte policy.
Records are scoped by both project AND selected source-file identity.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from zhibiao.core.cover_budget import bounded_cover_work


async def ensure_table(conn: Any) -> None:
    await conn.execute("""
        create table if not exists project_cover_sources (
            project_id uuid not null references projects(id) on delete cascade,
            file_id uuid not null references project_files(id) on delete cascade,
            plan jsonb not null,
            primary key (project_id, file_id)
        )
    """)


@bounded_cover_work
def plan_from_word(raw: bytes, filename: str) -> dict[str, Any] | None:
    from zhibiao.core.cover_word import word_cover_plan, word_style_plan

    return word_cover_plan(raw, filename) or word_style_plan(raw, filename)


async def store_word_cover(conn: Any, project_id: str, file_id: str,
                           raw: bytes, filename: str) -> bool:
    if not str(filename).lower().endswith((".docx", ".doc", ".wps", ".rtf")):
        return False
    plan = await asyncio.to_thread(plan_from_word, raw, filename)
    if not plan:
        return False
    encoded = dict(plan)
    if isinstance(encoded.get("docx"), bytes):
        encoded["docx_b64"] = base64.b64encode(encoded.pop("docx")).decode("ascii")
    await ensure_table(conn)
    await conn.execute("""
        insert into project_cover_sources(project_id, file_id, plan)
        values ($1::text::uuid, $2::text::uuid, $3::jsonb)
        on conflict(project_id, file_id) do update set plan=excluded.plan
    """, str(project_id), str(file_id), json.dumps(encoded, ensure_ascii=False))
    return True


async def load_word_cover(conn: Any, project_id: str, file_id: str) -> dict[str, Any] | None:
    # Missing artifact/table on older uploads is a normal fallback, not a reason
    # to use a different file or the first body-chapter slice as a cover.
    try:
        value = await conn.fetchval(
            "select plan from project_cover_sources where project_id::text=$1 and file_id::text=$2",
            str(project_id), str(file_id))
    except Exception:
        return None
    if not value:
        return None
    result = json.loads(value) if isinstance(value, str) else dict(value)
    if result.get("docx_b64"):
        result["docx"] = base64.b64decode(result.pop("docx_b64"))
    return result
