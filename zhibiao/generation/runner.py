from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select

from zhibiao.core.chapter_rules import (
    container_chapter_numbers,
    estimate_word_budgets,
    section_numbers,
    shape_chapters,
)
from zhibiao.corpus import tender_corpus
from zhibiao.database import session_factory
from zhibiao.domain import JobStatus, TenderStatus
from zhibiao.generation import engine
from zhibiao.generation.quality import run_quality
from zhibiao.llm import get_chat_client
from zhibiao.models import Analysis, BidDocument, BidSection, Job, Tender, TenderFile

logger = logging.getLogger(__name__)

# 兼容测试/外部注入的旧版单章生成钩子；生产默认走 generation.engine。
generate_body = None


def serialize_job(job: Job) -> dict:
    payload = job.payload or {}
    result = job.result or {}
    return {
        "id": job.id,
        "tenderId": job.tender_id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "payload": payload,
        "result": result,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
    }


class GenerationManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    async def start(
        self,
        tender_id: str,
        account: str,
        mode: str = "missing",
        section_ids: list[str] | None = None,
    ) -> Job:
        active = await self.active_job(tender_id)
        if active:
            raise RuntimeError("已有生成任务在进行，请先等待完成或取消")
        async with session_factory()() as db:
            document = (
                await db.execute(
                    select(BidDocument)
                    .where(BidDocument.tender_id == tender_id)
                    .order_by(BidDocument.created_at.desc())
                )
            ).scalar_one_or_none()
            if not document:
                raise RuntimeError("尚未生成标书大纲")
            rows = (
                await db.execute(
                    select(BidSection)
                    .where(BidSection.document_id == document.id)
                    .order_by(BidSection.position.asc())
                )
            ).scalars().all()
            selected = [row for row in rows if not section_ids or row.id in set(section_ids)]
            if not selected:
                raise RuntimeError("没有可生成的章节")
            job = Job(
                tender_id=tender_id,
                kind="write",
                status=JobStatus.QUEUED,
                progress=0,
                message="已排队，等待开始",
                payload={
                    "mode": mode,
                    "sectionIds": [row.id for row in selected],
                    "total": len(selected),
                    "account": account,
                },
            )
            db.add(job)
            await db.flush()
            await db.commit()
            job_id = job.id
        event = asyncio.Event()
        self._cancel[job_id] = event
        self._tasks[job_id] = asyncio.create_task(self._run(job_id, event))
        return job

    def cancel(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        if not event:
            return False
        event.set()
        return True

    async def active_job(self, tender_id: str) -> Job | None:
        async with session_factory()() as db:
            return (
                await db.execute(
                    select(Job)
                    .where(
                        Job.tender_id == tender_id,
                        Job.kind == "write",
                        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                    .order_by(Job.created_at.desc())
                )
            ).scalar_one_or_none()

    async def _update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        result: dict | None = None,
    ) -> None:
        async with session_factory()() as db:
            job = await db.get(Job, job_id)
            if not job:
                return
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress = max(0, min(100, progress))
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
            await db.commit()

    async def _run(self, job_id: str, cancel: asyncio.Event) -> None:
        errors: list[dict] = []
        try:
            async with session_factory()() as db:
                job = await db.get(Job, job_id)
                if not job:
                    return
                tender = await db.get(Tender, job.tender_id)
                if not tender:
                    await self._update(job_id, status=JobStatus.FAILED, message="项目不存在")
                    return
                document = (
                    await db.execute(
                        select(BidDocument)
                        .where(BidDocument.tender_id == tender.id)
                        .order_by(BidDocument.created_at.desc())
                    )
                ).scalar_one_or_none()
                if not document:
                    await self._update(job_id, status=JobStatus.FAILED, message="尚未生成标书大纲")
                    return
                sections = (
                    await db.execute(
                        select(BidSection)
                        .where(BidSection.document_id == document.id)
                        .order_by(BidSection.position.asc())
                    )
                ).scalars().all()
                selected_ids = set((job.payload or {}).get("sectionIds") or [])
                sections = [row for row in sections if not selected_ids or row.id in selected_ids]
                analysis = (
                    await db.execute(
                        select(Analysis)
                        .where(Analysis.tender_id == tender.id, Analysis.is_current.is_(True))
                        .order_by(Analysis.created_at.desc())
                    )
                ).scalar_one_or_none()
                mode = str((job.payload or {}).get("mode") or "missing")
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

            await self._update(
                job_id,
                status=JobStatus.RUNNING,
                progress=0,
                message=f"开始生成，共 {total} 节",
            )
            facts_ok, facts_reason = engine.check_facts(tender_row, analysis)
            if not facts_ok:
                logger.warning("招标事实不足，继续生成但正文只能使用占位：%s", facts_reason)
                await self._update(job_id, message="招标事实不足，正文将保留待填占位")

            client = await get_chat_client(assistant="writer")
            context = engine.build_context(tender_row, analysis, tender_text, pdf_bytes)
            chapters: list[dict] = []
            targets: list[dict] = []
            for section in sections:
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
                if mode == "missing":
                    wanted = not (body and section.status == "written")
                elif mode == "retry":
                    wanted = section.status == "failed" or not body
                else:
                    wanted = True
                if wanted:
                    targets.append(chapter)
            if not targets:
                await self._update(
                    job_id, status=JobStatus.SUCCEEDED, progress=100, message="没有待生成章节"
                )
                return

            shaped = shape_chapters(chapters)
            shaped_by_id = {item["id"]: item for item in shaped}
            shaped = await engine.plan_focus(
                client, shaped, context["ledger"], stop_event=cancel
            )
            numbers = section_numbers(shaped)
            containers = container_chapter_numbers(numbers.values())
            budgets = estimate_word_budgets(shaped, 0)
            await self._update(
                job_id,
                progress=4,
                message=f"全书写作计划已生成，开始写作 {len(targets)} 节",
            )

            generated = 0
            generated_ids: set[str] = set()
            failed_ids: set[str] = set()
            done = 0
            sem = asyncio.Semaphore(2)
            sections_by_id = {row.id: row for row in sections}

            async def _one(chapter: dict) -> None:
                nonlocal generated, done
                if cancel.is_set():
                    return
                async with sem:
                    if cancel.is_set():
                        return
                    await self._update(
                        job_id,
                        message=f"正在生成：{chapter['title']}",
                    )
                    try:
                        if generate_body is not None:
                            text = await generate_body(
                                client,
                                tender_row,
                                analysis,
                                sections_by_id.get(chapter["id"]),
                                sections,
                            )
                            _source, error = "", ""
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
                                stop_event=cancel,
                            )
                    except Exception as exc:  # noqa: BLE001
                        text, _, error = "", "", f"生成异常：{exc}"
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
                        await self._update(
                            job_id,
                            progress=int(done / max(1, len(targets)) * 92) + 4,
                            message=f"未成功 {done}/{len(targets)} 节：{chapter['title']}",
                        )
                        return
                    async with session_factory()() as db:
                        row = await db.get(BidSection, chapter["id"])
                        tender_row_db = await db.get(Tender, tender_row.id)
                        if row:
                            row.body = text
                            row.status = "written"
                            row.target_words = int(budgets.get(chapter["id"], row.target_words) or row.target_words)
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
                    await self._update(
                        job_id,
                        progress=int(done / max(1, len(targets)) * 92) + 4,
                        message=f"已完成 {done}/{len(targets)} 节：{chapter['title']}",
                    )

            for pass_no in range(1, 3):
                if cancel.is_set():
                    break
                pending = [
                    item for item in targets if item["id"] not in generated_ids
                ]
                if not pending:
                    break
                if pass_no > 1:
                    await self._update(
                        job_id, message=f"第 {pass_no} 轮补齐：{len(pending)} 节"
                    )
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
            result = {
                "generated": generated,
                "failed": failed,
                "skipped": max(0, total - generated - failed),
                "errors": final_errors,
                "quality": quality["stats"],
                "issues": (final_issues + quality["issues"])[:40],
            }
            await self._update(
                job_id,
                status=JobStatus.SUCCEEDED,
                progress=100,
                message=f"生成完成：成功 {generated} 节，失败 {failed} 节",
                result=result,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成任务异常: %s", exc)
            await self._update(job_id, status=JobStatus.FAILED, message=f"生成失败：{exc}")
        finally:
            self._cancel.pop(job_id, None)
            self._tasks.pop(job_id, None)


async def fail_stale_jobs() -> None:
    """服务重启后，把遗留的排队/运行中任务标记为失败，避免前端误判为仍在生成。"""
    async with session_factory()() as db:
        rows = (
            await db.execute(
                select(Job).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            )
        ).scalars().all()
        for job in rows:
            job.status = JobStatus.FAILED
            job.message = "服务重启，生成任务已中断，请重新启动生成"
        if rows:
            await db.commit()


manager = GenerationManager()
