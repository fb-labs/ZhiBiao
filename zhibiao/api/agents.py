from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.agents.catalog import AGENTS
from zhibiao.agents.facts import load_facts
from zhibiao.database import get_db
from zhibiao.models import Account, Tender
from zhibiao.security import require_permission

router = APIRouter(prefix="/agents", tags=["v2-agents"])
tender_router = APIRouter(prefix="/tenders", tags=["v2-agents"])


@router.get("")
async def list_agents(_: Account = Depends(require_permission("tender.read"))):
    return {"items": [spec.as_dict() for spec in AGENTS.values()]}


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
