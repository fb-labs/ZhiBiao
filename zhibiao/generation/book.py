"""全书生成：逐章写作、失败补齐、全书后处理。

这是"正文编制"这一步的唯一实现：正文编制 Agent 的 write_book 工具与
生成任务的兜底都调用这里，进度、取消、失败重试的语义保持不变。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from zhibiao.core.chapter_rules import (
    container_chapter_numbers,
    estimate_word_budgets,
    section_numbers,
    shape_chapters,
)
from zhibiao.corpus import tender_corpus
from zhibiao.database import session_factory
from zhibiao.domain import TenderStatus
from zhibiao.generation import engine
from zhibiao.generation.quality import run_quality
from zhibiao.llm import get_chat_client
from zhibiao.models import Analysis, BidDocument, BidSection, Tender, TenderFile

logger = logging.getLogger(__name__)

Progress = Callable[[int, str], Awaitable[None]]


async def _noop(_percent: int, _message: str) -> None:
    return None


async def run_book(
    *,
    tender_id: str,
    section_ids: list[str] | None = None,
    mode: str = "missing",
    cancel: asyncio.Event | None = None,
    progress: Progress = _noop,
    body_writer: Any = None,
) -> dict[str, Any]:
    """生成一本书：返回 generated/failed/skipped/errors/quality/issues。"""
    stop = cancel or asyncio.Event()
    async with session_factory()() as db:
        tender = await db.get(Tender, tender_id)
        if not tender:
            raise RuntimeError("项目不存在")
        document = (
            await db.execute(
                select(BidDocument)
                .where(BidDocument.tender_id == tender.id)
                .order_by(BidDocument.created_at.desc())
            )
        ).scalar_one_or_none()
        if not document:
            raise RuntimeError("尚未生成标书大纲")
        sections = (
            await db.execute(
                select(BidSection)
                .where(BidSection.document_id == document.id)
                .order_by(BidSection.position.asc())
            )
        ).scalars().all()
        selected_ids = set(section_ids or [])
        # 单章重写也要用全书章节算编号/上下文，否则会写出「1.1」这种错号。
        all_sections = list(sections)
        sections = [row for row in sections if not selected_ids or row.id in selected_ids]
        analysis = (
            await db.execute(
                select(Analysis)
                .where(Analysis.tender_id == tender.id, Analysis.is_current.is_(True))
                .order_by(Analysis.created_at.desc())
            )
        ).scalar_one_or_none()
        total = len(sections)
        tender_row = tender
        document_id = document.id
        tender_text = await tender_corpus(db, tender, limit=160000)
        pdf_bytes = None
        source_file = (
            await db.execute(
                select(TenderFile)
                .where(TenderFile.tender_id == tender.id)
                .order_by(TenderFile.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            source_file
            and source_file.filename.lower().endswith(".pdf")
            and Path(source_file.stored_path).is_file()
        ):
            pdf_bytes = Path(source_file.stored_path).read_bytes()

    await progress(0, f"开始生成，共 {total} 节")
    facts_ok, facts_reason = engine.check_facts(tender_row, analysis)
    if not facts_ok:
        logger.warning("招标事实不足，继续生成但正文只能使用占位：%s", facts_reason)
        await progress(0, "招标事实不足，正文将保留待填占位")

    client = await get_chat_client(assistant="writer")
    context = engine.build_context(tender_row, analysis, tender_text, pdf_bytes)
    chapters: list[dict] = []
    targets: list[dict] = []
    for section in all_sections:
        body = (section.body or "").strip()
        chapter = {
            "id": section.id,
            "seq": section.position + 1,
            "level": 2 if section.parent_key else 1,
            "title": section.heading,
            "content": body,
            "status": "已生成" if body and section.status == "written" else "未生成",
            "target_words": section.target_words or 0,
            "writing_focus": "",
            "project_name": tender_row.title,
        }
        chapters.append(chapter)
        if selected_ids and section.id not in selected_ids:
            continue
        if mode == "missing":
            wanted = not (body and section.status == "written")
        elif mode == "retry":
            wanted = section.status == "failed" or not body
        else:
            wanted = True
        if wanted:
            targets.append(chapter)
    if not targets:
        await progress(100, "没有待生成章节")
        return {
            "generated": 0,
            "failed": 0,
            "skipped": total,
            "errors": [],
            "quality": {},
            "issues": [],
        }

    shaped = shape_chapters(chapters)
    shaped_by_id = {item["id"]: item for item in shaped}
    shaped = await engine.plan_focus(client, shaped, context["ledger"], stop_event=stop)
    numbers = section_numbers(shaped)
    containers = container_chapter_numbers(numbers.values())
    budgets = estimate_word_budgets(shaped, 0)
    await progress(4, f"全书写作计划已生成，开始写作 {len(targets)} 节")

    errors: list[dict] = []
    generated = 0
    generated_ids: set[str] = set()
    failed_ids: set[str] = set()
    done = 0
    sem = asyncio.Semaphore(2)
    sections_by_id = {row.id: row for row in all_sections}

    async def _one(chapter: dict) -> None:
        nonlocal generated, done
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            await progress(4 + int(done / max(1, len(targets)) * 92), f"正在生成：{chapter['title']}")
            try:
                if body_writer is not None:
                    text = await body_writer(
                        client,
                        tender_row,
                        analysis,
                        sections_by_id.get(chapter["id"]),
                        sections,
                    )
                    error = ""
                else:
                    text, _source, error = await engine.generate_chapter(
                        client,
                        shaped_by_id.get(chapter["id"], chapter),
                        context=context,
                        chapters=shaped,
                        numbers=numbers,
                        containers=containers,
                        budgets=budgets,
                        issues=[],
                        stop_event=stop,
                    )
            except Exception as exc:  # noqa: BLE001
                text, error = "", f"生成异常：{exc}"
            done += 1
            if error or not text.strip():
                failed_ids.add(chapter["id"])
                errors.append(
                    {
                        "sectionId": chapter["id"],
                        "heading": chapter["title"],
                        "error": error or "模型未返回正文",
                    }
                )
                async with session_factory()() as db:
                    row = await db.get(BidSection, chapter["id"])
                    if row:
                        row.status = "failed"
                        await db.commit()
                await progress(
                    4 + int(done / max(1, len(targets)) * 92),
                    f"未成功 {done}/{len(targets)} 节：{chapter['title']}",
                )
                return
            async with session_factory()() as db:
                row = await db.get(BidSection, chapter["id"])
                tender_row_db = await db.get(Tender, tender_row.id)
                if row:
                    row.body = text
                    row.status = "written"
                    row.target_words = int(
                        budgets.get(chapter["id"], row.target_words) or row.target_words
                    )
                if tender_row_db and tender_row_db.status in {
                    TenderStatus.OUTLINED,
                    TenderStatus.DRAFTING,
                }:
                    tender_row_db.status = TenderStatus.DRAFTING
                await db.commit()
            chapter["content"] = text
            generated_ids.add(chapter["id"])
            failed_ids.discard(chapter["id"])
            generated += 1
            await progress(
                4 + int(done / max(1, len(targets)) * 92),
                f"已完成 {done}/{len(targets)} 节：{chapter['title']}",
            )

    for pass_no in range(1, 3):
        if stop.is_set():
            break
        pending = [item for item in targets if item["id"] not in generated_ids]
        if not pending:
            break
        if pass_no > 1:
            await progress(4 + int(done / max(1, len(targets)) * 92), f"第 {pass_no} 轮补齐：{len(pending)} 节")
        before = generated
        await asyncio.gather(*(_one(item) for item in pending), return_exceptions=True)
        if generated == before:
            break

    async with session_factory()() as db:
        rows = (
            await db.execute(
                select(BidSection)
                .where(BidSection.document_id == document_id)
                .order_by(BidSection.position.asc())
            )
        ).scalars().all()
    row_dicts = [
        {
            "id": row.id,
            "seq": row.position + 1,
            "level": 2 if row.parent_key else 1,
            "title": row.heading,
            "content": row.body or "",
        }
        for row in rows
        if (row.body or "").strip()
    ]
    finalized, final_issues = engine.finalize_document(row_dicts, tender_text)
    quality = run_quality(list(rows))
    merged_updates = {**finalized, **quality["updates"]}
    if merged_updates:
        async with session_factory()() as db:
            for section_id, body in merged_updates.items():
                row = await db.get(BidSection, section_id)
                if row:
                    row.body = body
            await db.commit()

    failed = len(failed_ids)
    final_errors = [item for item in errors if item.get("sectionId") in failed_ids]
    return {
        "generated": generated,
        "failed": failed,
        "skipped": max(0, total - generated - failed),
        "errors": final_errors[:20],
        "quality": quality["stats"],
        "issues": (final_issues + quality["issues"])[:40],
    }
