from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from zhibiao.database import session_factory
from zhibiao.domain import JobStatus, TenderStatus
from zhibiao.generation.quality import run_quality
from zhibiao.generation.writer import generate_body
from zhibiao.llm import get_chat_client
from zhibiao.models import Analysis, BidDocument, BidSection, Job, Tender

logger = logging.getLogger(__name__)


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

            await self._update(
                job_id,
                status=JobStatus.RUNNING,
                progress=0,
                message=f"开始生成，共 {total} 节",
            )
            client = await get_chat_client()
            done = 0
            failed = 0
            generated = 0

            for section in sections:
                if cancel.is_set():
                    await self._update(job_id, status=JobStatus.CANCELED, message="已取消生成")
                    return
                if mode == "missing" and (section.body or "").strip() and section.status == "written":
                    done += 1
                    await self._update(
                        job_id,
                        progress=int(done / max(1, total) * 100),
                        message=f"跳过已完成章节：{section.heading}",
                    )
                    continue
                if mode == "retry" and section.status != "failed":
                    done += 1
                    continue

                await self._update(
                    job_id,
                    progress=int(done / max(1, total) * 100),
                    message=f"正在生成：{section.heading}",
                )
                text = ""
                last_error = ""
                for attempt in range(2):
                    try:
                        text = await generate_body(client, tender_row, analysis, section, sections)
                        if text.strip():
                            break
                        last_error = "模型未返回内容"
                    except Exception as exc:  # noqa: BLE001
                        last_error = str(exc)
                        logger.warning("章节生成失败（%s，第 %s 次）：%s", section.heading, attempt + 1, exc)
                        if attempt == 0:
                            await asyncio.sleep(3)
                if not text.strip():
                    failed += 1
                    async with session_factory()() as db:
                        row = await db.get(BidSection, section.id)
                        if row:
                            row.status = "failed"
                            await db.commit()
                    errors.append({"sectionId": section.id, "heading": section.heading, "error": last_error})
                else:
                    async with session_factory()() as db:
                        row = await db.get(BidSection, section.id)
                        tender = await db.get(Tender, tender_row.id)
                        if row:
                            row.body = text
                            row.status = "written"
                            generated += 1
                        if tender and tender.status in {TenderStatus.OUTLINED, TenderStatus.DRAFTING}:
                            tender.status = TenderStatus.DRAFTING
                        await db.commit()
                    section.body = text
                    section.status = "written"
                done += 1
                await self._update(
                    job_id,
                    progress=int(done / max(1, total) * 100),
                    message=f"已完成 {done}/{total} 节：{section.heading}",
                )

            async with session_factory()() as db:
                rows = (
                    await db.execute(
                        select(BidSection)
                        .where(BidSection.document_id == document_id)
                        .order_by(BidSection.position.asc())
                    )
                ).scalars().all()
            quality = run_quality(list(rows))
            if quality["updates"]:
                async with session_factory()() as db:
                    for section_id, body in quality["updates"].items():
                        row = await db.get(BidSection, section_id)
                        if row:
                            row.body = body
                    await db.commit()

            result = {
                "generated": generated,
                "failed": failed,
                "skipped": max(0, total - generated - failed),
                "errors": errors,
                "quality": quality["stats"],
                "issues": quality["issues"][:40],
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
