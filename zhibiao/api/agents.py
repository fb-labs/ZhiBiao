from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.agents.catalog import AGENTS
from zhibiao.agents.facts import load_facts
from zhibiao.agents.service import agent_manager
from zhibiao.database import get_db
from zhibiao.generation.runner import serialize_job
from zhibiao.models import Account, Job, Tender
from zhibiao.security import require_permission

router = APIRouter(prefix="/agents", tags=["v2-agents"])
tender_router = APIRouter(prefix="/tenders", tags=["v2-agents"])
run_router = APIRouter(prefix="/agent-runs", tags=["v2-agents"])


@router.get("")
async def list_agents(_: Account = Depends(require_permission("tender.read"))):
    return {"items": [spec.as_dict() for spec in AGENTS.values()]}


@tender_router.post("/{tender_id}/agents/{key}/run")
async def run_agent(
    tender_id: str,
    key: str,
    payload: dict | None = Body(default=None),
    account: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    if key not in AGENTS:
        raise HTTPException(status_code=404, detail="未知 Agent")
    if not await db.get(Tender, tender_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        job = await agent_manager.start(tender_id, key, account.username, payload or {})
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_job(job)


@tender_router.get("/{tender_id}/agents/facts")
async def get_facts(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    tender = await db.get(Tender, tender_id)
    if not tender:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"facts": load_facts(tender)}


@tender_router.get("/{tender_id}/agent-runs")
async def list_runs(
    tender_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    active = await agent_manager.active_job(tender_id)
    rows = (
        await db.execute(
            select(Job)
            .where(Job.tender_id == tender_id, Job.kind.like("agent:%"))
            .order_by(Job.created_at.desc())
            .limit(12)
        )
    ).scalars().all()
    return {
        "active": serialize_job(active) if active else None,
        "items": [serialize_job(row) for row in rows],
    }


@run_router.get("/{job_id}")
async def get_run(
    job_id: str,
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Job, job_id)
    if not row or not row.kind.startswith("agent:"):
        raise HTTPException(status_code=404, detail="Agent 运行记录不存在")
    return serialize_job(row)


@run_router.post("/{job_id}/cancel")
async def cancel_run(
    job_id: str,
    _: Account = Depends(require_permission("document.write")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Job, job_id)
    if not row or not row.kind.startswith("agent:"):
        raise HTTPException(status_code=404, detail="Agent 运行记录不存在")
    if not agent_manager.cancel(job_id):
        raise HTTPException(status_code=400, detail="任务已结束，无法取消")
    return {"ok": True}
