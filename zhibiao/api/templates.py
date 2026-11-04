from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.database import get_db
from zhibiao.models import Account, Template
from zhibiao.security import require_permission

router = APIRouter(prefix="/templates", tags=["v2-templates"])

DEFAULT_CONFIG = {
    "pageMargin": "2.5cm",
    "bodyFont": "宋体",
    "bodySize": 12,
    "lineSpacing": 1.5,
    "headingFont": "黑体",
    "coverTitleSize": 22,
}


class TemplatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    config: dict = Field(default_factory=dict)
    isDefault: bool = False


def serialize(row: Template) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "config": row.config or {},
        "isDefault": row.is_default,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
async def list_templates(
    _: Account = Depends(require_permission("tender.read")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(select(Template).order_by(Template.created_at.asc()))
    ).scalars().all()
    return {"items": [serialize(row) for row in rows], "defaults": DEFAULT_CONFIG}


@router.post("")
async def create_template(
    payload: TemplatePayload,
    _: Account = Depends(require_permission("admin.templates")),
    db: AsyncSession = Depends(get_db),
):
    if payload.isDefault:
        await db.execute(update(Template).values(is_default=False))
    row = Template(
        name=payload.name.strip(),
        description=payload.description.strip(),
        config={**DEFAULT_CONFIG, **(payload.config or {})},
        is_default=payload.isDefault,
    )
    db.add(row)
    await db.flush()
    return serialize(row)


@router.put("/{template_id}")
async def update_template(
    template_id: str,
    payload: TemplatePayload,
    _: Account = Depends(require_permission("admin.templates")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Template, template_id)
    if not row:
        raise HTTPException(status_code=404, detail="模板不存在")
    if payload.isDefault:
        await db.execute(update(Template).values(is_default=False))
    row.name = payload.name.strip()
    row.description = payload.description.strip()
    row.config = {**DEFAULT_CONFIG, **(payload.config or {})}
    row.is_default = payload.isDefault
    await db.flush()
    return serialize(row)


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    _: Account = Depends(require_permission("admin.templates")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Template, template_id)
    if not row:
        raise HTTPException(status_code=404, detail="模板不存在")
    await db.delete(row)
    return {"ok": True}
