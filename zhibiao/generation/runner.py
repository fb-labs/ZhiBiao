from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from zhibiao.database import session_factory
from zhibiao.domain import JobStatus
from zhibiao.generation.book import run_book
from zhibiao.models import BidDocument, BidSection, Job, Tender

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

    def cancel_event(self, job_id: str) -> asyncio.Event | None:
        """给正文编制 Agent 的 write_book 工具取取消信号。"""
        return self._cancel.get(job_id)

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
        try:
            async with session_factory()() as db:
                job = await db.get(Job, job_id)
                if not job:
                    return
                tender_id = job.tender_id
                payload = dict(job.payload or {})
                if not await db.get(Tender, tender_id):
                    await self._update(job_id, status=JobStatus.FAILED, message="项目不存在")
                    return
            mode = str(payload.get("mode") or "missing")

            async def progress(percent: int, message: str) -> None:
                if cancel.is_set():
                    raise RuntimeError("__cancelled__")
                await self._update(job_id, progress=percent, message=message, status=JobStatus.RUNNING)

            # 这一步的执行者是正文编制 Agent：它调用 write_book 工具跑全书写作。
            from zhibiao.agents.catalog import BOOK_WRITER
            from zhibiao.agents.steps import run_step_agent

            await self._update(
                job_id, status=JobStatus.RUNNING, progress=0, message="正文编制 Agent 启动"
            )
            outcome = await run_step_agent(
                "book_writer",
                tender_id=tender_id,
                task="按已确定的大纲与招标事实逐节写作，完成后做全书后处理。",
                payload={"mode": mode, "tenderId": tender_id},
                account=str(payload.get("account") or "agent"),
                job_id=job_id,
                on_progress=progress,
                spec=BOOK_WRITER,
            )
            result = dict(outcome.get("output") or {})
            if outcome.get("error") or "generated" not in result:
                # Agent 没跑通（模型不可用/工具未调用）时回落到同一份实现
                result = await run_book(
                    tender_id=tender_id,
                    section_ids=payload.get("sectionIds"),
                    mode=mode,
                    cancel=cancel,
                    progress=progress,
                    body_writer=generate_body,
                )
            result["agent"] = "正文编制 Agent"
            result["trace"] = outcome.get("trace") or []
            await self._update(
                job_id,
                status=JobStatus.SUCCEEDED,
                progress=100,
                message=f"生成完成：成功 {result.get('generated', 0)} 节，失败 {result.get('failed', 0)} 节",
                result=result,
            )
        except RuntimeError as exc:
            if str(exc) == "__cancelled__":
                await self._update(job_id, status=JobStatus.CANCELED, progress=100, message="已取消")
            else:
                logger.exception("生成任务异常: %s", exc)
                await self._update(job_id, status=JobStatus.FAILED, message=f"生成失败：{exc}")
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
