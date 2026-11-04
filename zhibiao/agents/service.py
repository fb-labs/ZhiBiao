from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from zhibiao.agents.base import AgentContext, AgentRunner
from zhibiao.agents.catalog import AGENTS
from zhibiao.agents.facts import assign_facts, load_facts
from zhibiao.agents.tools import TOOLS
from zhibiao.database import session_factory
from zhibiao.domain import JobStatus
from zhibiao.generation.runner import serialize_job
from zhibiao.models import Job, Tender

logger = logging.getLogger(__name__)


class AgentManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    async def active_job(self, tender_id: str) -> Job | None:
        async with session_factory()() as db:
            return (
                await db.execute(
                    select(Job)
                    .where(
                        Job.tender_id == tender_id,
                        Job.kind.like("agent:%"),
                        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                    .order_by(Job.created_at.desc())
                )
            ).scalar_one_or_none()

    async def start(
        self,
        tender_id: str,
        key: str,
        account: str,
        payload: dict | None = None,
    ) -> Job:
        spec = AGENTS.get(key)
        if not spec:
            raise RuntimeError("未知 Agent")
        active = await self.active_job(tender_id)
        if active:
            raise RuntimeError("已有 Agent 任务在进行，请先等待完成或取消")
        async with session_factory()() as db:
            job = Job(
                tender_id=tender_id,
                kind=f"agent:{key}",
                status=JobStatus.QUEUED,
                progress=0,
                message=f"{spec.name} 已排队",
                payload={"agent": key, "taskPayload": payload or {}, "account": account},
            )
            db.add(job)
            await db.flush()
            await db.commit()
            job_id = job.id
        event = asyncio.Event()
        self._cancel[job_id] = event
        self._tasks[job_id] = asyncio.create_task(self._run(job_id, key, event, payload or {}))
        return job

    def cancel(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        if not event:
            return False
        event.set()
        return True

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

    async def _run(self, job_id: str, key: str, cancel: asyncio.Event, payload: dict) -> None:
        spec = AGENTS[key]
        try:
            await self._update(job_id, status=JobStatus.RUNNING, progress=2, message=f"{spec.name} 启动")
            account = "agent"
            async with session_factory()() as db:
                job = await db.get(Job, job_id)
                if job:
                    account = str((job.payload or {}).get("account") or "agent")

            async def progress(percent: int, message: str) -> None:
                if cancel.is_set():
                    raise RuntimeError("__cancelled__")
                await self._update(job_id, progress=percent, message=message)

            context = AgentContext(
                tender_id=str((await self._tender_id(job_id)) or ""),
                job_id=job_id,
                account=account,
                on_progress=progress,
            )
            if key == "tender_reader":
                digest = await TOOLS["tender_digest"].handler(context, {})
                if digest.get("digest"):
                    payload = {**payload, "tenderDigest": digest["digest"]}
            task = str(payload.get("task") or spec.description)
            runner = AgentRunner()
            outcome = await runner.run(spec, context, task, payload)
            if cancel.is_set():
                await self._update(
                    job_id,
                    status=JobStatus.CANCELED,
                    progress=100,
                    message=f"{spec.name} 已取消",
                    result=outcome,
                )
                return
            if key == "tender_reader":
                output = outcome.get("output") or {}
                raw = output.get("facts") if isinstance(output.get("facts"), dict) else output
                if isinstance(raw, dict) and raw.get("project"):
                    async with session_factory()() as db:
                        tender = await db.get(Tender, context.tender_id)
                        if tender and not load_facts(tender):
                            assign_facts(tender, raw)
                            await db.commit()
            status = JobStatus.FAILED if outcome.get("error") else JobStatus.SUCCEEDED
            await self._update(
                job_id,
                status=status,
                progress=100,
                message=f"{spec.name} 完成" if status == JobStatus.SUCCEEDED else f"{spec.name} 结束：{outcome.get('error')}",
                result=outcome,
            )
        except RuntimeError as exc:
            if str(exc) == "__cancelled__":
                await self._update(
                    job_id,
                    status=JobStatus.CANCELED,
                    progress=100,
                    message=f"{spec.name} 已取消",
                    result={"error": "用户取消"},
                )
            else:
                logger.exception("%s 执行失败", spec.name)
                await self._update(
                    job_id,
                    status=JobStatus.FAILED,
                    message=f"{spec.name} 失败：{exc}",
                    result={"error": str(exc)},
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s 执行失败", spec.name)
            await self._update(
                job_id,
                status=JobStatus.FAILED,
                message=f"{spec.name} 失败：{exc}",
                result={"error": str(exc)},
            )
        finally:
            self._cancel.pop(job_id, None)
            self._tasks.pop(job_id, None)

    async def _tender_id(self, job_id: str) -> str | None:
        async with session_factory()() as db:
            job = await db.get(Job, job_id)
            return job.tender_id if job else None


agent_manager = AgentManager()
