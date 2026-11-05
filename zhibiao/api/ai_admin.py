from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.capabilities import load_capabilities, normalize_capabilities, save_capabilities
from zhibiao.database import get_db
from zhibiao.llm import ChatClient
from zhibiao.models import Account, AiProvider, Assistant, AuditEvent
from zhibiao.security import require_permission

router = APIRouter(prefix="/admin", tags=["v2-admin"])


class ProviderPayload(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = ""
    model: str = Field(default="", max_length=80)
    temperature: float = Field(default=0.6, ge=0, le=2)
    max_tokens: int = Field(default=4000, ge=64, le=32000)
    is_active: bool = True
    is_default: bool = False


class AssistantPayload(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=80)
    purpose: str = ""
    provider_id: str | None = None
    model: str = ""
    temperature: float = Field(default=0.6, ge=0, le=2)
    max_tokens: int = Field(default=4000, ge=64, le=32000)
    enabled: bool = True


class OcrCapabilityPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    base_url: str = Field(default="", max_length=255, alias="baseUrl")
    api_key: str = Field(default="", alias="apiKey")
    model: str = Field(default="", max_length=80)
    max_pages: int = Field(default=10, ge=1, le=50, alias="maxPages")


class ImageCapabilityPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    base_url: str = Field(default="", max_length=255, alias="baseUrl")
    api_key: str = Field(default="", alias="apiKey")
    model: str = Field(default="", max_length=80)
    size: str = Field(default="1024x1024", max_length=20)
    max_per_section: int = Field(default=2, ge=0, le=8, alias="maxPerSection")
    max_per_book: int = Field(default=8, ge=0, le=60, alias="maxPerBook")


class CapabilitiesPayload(BaseModel):
    ocr: OcrCapabilityPayload = Field(default_factory=OcrCapabilityPayload)
    image: ImageCapabilityPayload = Field(default_factory=ImageCapabilityPayload)


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "••••"
    return f"{key[:4]}••••{key[-4:]}"


def _provider(row: AiProvider) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "baseUrl": row.base_url,
        "apiKey": _mask(row.api_key),
        "apiKeySet": bool(row.api_key),
        "model": row.model,
        "temperature": row.temperature,
        "maxTokens": row.max_tokens,
        "isActive": row.is_active,
        "isDefault": row.is_default,
    }


def _assistant(row: Assistant) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "label": row.label,
        "purpose": row.purpose,
        "providerId": row.provider_id,
        "model": row.model,
        "temperature": row.temperature,
        "maxTokens": row.max_tokens,
        "enabled": row.enabled,
    }


def _capability_view(data: dict) -> dict:
    view = normalize_capabilities(data)
    for kind in ("ocr", "image"):
        cfg = dict(view[kind])
        cfg["apiKeySet"] = bool(cfg.get("apiKey"))
        cfg["apiKey"] = _mask(cfg.get("apiKey", ""))
        cfg["ready"] = bool(cfg.get("enabled") and cfg.get("baseUrl"))
        view[kind] = cfg
    return view


@router.get("/capabilities")
async def get_capabilities(
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    data = await load_capabilities(db)
    return _capability_view(data)


@router.put("/capabilities")
async def update_capabilities(
    payload: CapabilitiesPayload,
    account: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    current = await load_capabilities(db)
    values = payload.model_dump(by_alias=True)
    # 密钥留空表示保持不变，避免前端回填掩码后把真实 Key 覆盖成 ••••
    for kind in ("ocr", "image"):
        if not values[kind].get("apiKey"):
            values[kind]["apiKey"] = current[kind].get("apiKey", "")
    data = await save_capabilities(db, values, actor=account.username)
    return _capability_view(data)


@router.get("/providers")
async def list_providers(
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(AiProvider).order_by(AiProvider.created_at))).scalars().all()
    return {"items": [_provider(row) for row in rows]}


@router.post("/providers")
async def create_provider(
    payload: ProviderPayload,
    account: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    if payload.is_default:
        await db.execute(update(AiProvider).values(is_default=False))
    row = AiProvider(
        label=payload.label,
        base_url=payload.base_url.rstrip("/"),
        api_key=payload.api_key,
        model=payload.model,
        temperature=payload.temperature,
        max_tokens=payload.max_tokens,
        is_active=payload.is_active,
        is_default=payload.is_default,
    )
    db.add(row)
    db.add(
        AuditEvent(
            actor=account.username,
            action="ai.provider.create",
            target_type="provider",
            target_id=row.id,
            detail={"label": row.label},
        )
    )
    await db.flush()
    return _provider(row)


@router.put("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    payload: ProviderPayload,
    account: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(AiProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    if payload.is_default:
        await db.execute(update(AiProvider).values(is_default=False))
    row.label = payload.label
    row.base_url = payload.base_url.rstrip("/")
    if payload.api_key:
        row.api_key = payload.api_key
    row.model = payload.model
    row.temperature = payload.temperature
    row.max_tokens = payload.max_tokens
    row.is_active = payload.is_active
    row.is_default = payload.is_default
    db.add(
        AuditEvent(
            actor=account.username,
            action="ai.provider.update",
            target_type="provider",
            target_id=row.id,
            detail={"label": row.label},
        )
    )
    await db.flush()
    return _provider(row)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: str,
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(AiProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    await db.delete(row)
    return {"ok": True}


@router.post("/providers/{provider_id}/probe")
async def probe_provider(
    provider_id: str,
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(AiProvider, provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    started = time.monotonic()
    client = ChatClient(row.base_url, row.api_key, row.model or "glm-5.2", timeout=30)
    try:
        reply = await client.chat(
            [{"role": "user", "content": "只回复两个字：可用"}],
            temperature=0,
            max_tokens=32,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "latencyMs": int((time.monotonic() - started) * 1000),
            "error": str(exc),
        }
    return {
        "ok": True,
        "latencyMs": int((time.monotonic() - started) * 1000),
        "reply": reply.strip()[:50],
    }


@router.get("/assistants")
async def list_assistants(
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(Assistant).order_by(Assistant.created_at))).scalars().all()
    return {"items": [_assistant(row) for row in rows]}


@router.post("/assistants")
async def create_assistant(
    payload: AssistantPayload,
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(Assistant).where(Assistant.key == payload.key))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="助手标识已存在")
    row = Assistant(**payload.model_dump())
    db.add(row)
    await db.flush()
    return _assistant(row)


@router.put("/assistants/{assistant_id}")
async def update_assistant(
    assistant_id: str,
    payload: AssistantPayload,
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Assistant, assistant_id)
    if not row:
        raise HTTPException(status_code=404, detail="助手不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await db.flush()
    return _assistant(row)


@router.delete("/assistants/{assistant_id}")
async def delete_assistant(
    assistant_id: str,
    _: Account = Depends(require_permission("admin.ai")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Assistant, assistant_id)
    if not row:
        raise HTTPException(status_code=404, detail="助手不存在")
    await db.delete(row)
    return {"ok": True}
