from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.database import get_db
from zhibiao.domain import PERMISSION_CATALOG, ROLE_CATALOG, ROLE_PERMISSIONS
from zhibiao.models import Account, AccountRole, AuditEvent, Role, RolePermission
from zhibiao.security import password_digest, require_permission

router = APIRouter(prefix="/admin", tags=["v2-accounts"])


class AccountCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[\w.\-]+$")
    password: str = Field(min_length=6, max_length=72)
    displayName: str = Field(default="", max_length=100)
    email: str | None = None
    roles: list[str] = Field(default_factory=list)
    isSuperuser: bool = False


class AccountUpdate(BaseModel):
    displayName: str | None = Field(default=None, max_length=100)
    email: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=72)
    roles: list[str] | None = None
    isActive: bool | None = None
    isSuperuser: bool | None = None


async def _roles_of(db: AsyncSession, account_id: str) -> list[str]:
    return list(
        (
            await db.execute(
                select(AccountRole.role_code).where(AccountRole.account_id == account_id)
            )
        ).scalars().all()
    )


async def _serialize(db: AsyncSession, row: Account) -> dict:
    return {
        "id": row.id,
        "username": row.username,
        "email": row.email,
        "displayName": row.display_name,
        "isActive": row.is_active,
        "isSuperuser": row.is_superuser,
        "roles": await _roles_of(db, row.id),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


async def _apply_roles(db: AsyncSession, account_id: str, roles: list[str]) -> None:
    valid = {role.code for role in (await db.execute(select(Role))).scalars().all()}
    picked = [code for code in dict.fromkeys(roles) if code in valid]
    await db.execute(delete(AccountRole).where(AccountRole.account_id == account_id))
    for code in picked:
        db.add(AccountRole(account_id=account_id, role_code=code))


@router.get("/permissions")
async def permissions_catalog(
    _: Account = Depends(require_permission("admin.accounts")),
):
    return {
        "permissions": [
            {"code": code, "name": name, "group": group}
            for code, name, group in PERMISSION_CATALOG
        ]
    }


@router.get("/roles")
async def list_roles(
    _: Account = Depends(require_permission("admin.accounts")),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(Role))).scalars().all()
    stored: dict[str, list[str]] = {}
    for row in (await db.execute(select(RolePermission))).scalars().all():
        stored.setdefault(row.role_code, []).append(row.permission_code)
    return {
        "items": [
            {
                "code": row.code,
                "name": row.name,
                "description": row.description,
                "permissions": stored.get(row.code, ROLE_PERMISSIONS.get(row.code, [])),
            }
            for row in rows
        ]
        if rows
        else [
            {
                "code": code,
                "name": name,
                "description": description,
                "permissions": ROLE_PERMISSIONS.get(code, []),
            }
            for code, name, description in ROLE_CATALOG
        ]
    }


@router.get("/accounts")
async def list_accounts(
    _: Account = Depends(require_permission("admin.accounts")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(select(Account).order_by(Account.created_at.asc()))
    ).scalars().all()
    return {"items": [await _serialize(db, row) for row in rows]}


@router.post("/accounts")
async def create_account(
    payload: AccountCreate,
    actor: Account = Depends(require_permission("admin.accounts")),
    db: AsyncSession = Depends(get_db),
):
    exists = await db.scalar(select(Account.id).where(Account.username == payload.username))
    if exists:
        raise HTTPException(status_code=400, detail="账号已存在")
    if payload.email:
        email_exists = await db.scalar(select(Account.id).where(Account.email == payload.email))
        if email_exists:
            raise HTTPException(status_code=400, detail="邮箱已被使用")
    row = Account(
        username=payload.username.strip(),
        email=payload.email or None,
        display_name=(payload.displayName or payload.username).strip(),
        password_hash=password_digest(payload.password),
        is_active=True,
        is_superuser=payload.isSuperuser,
    )
    db.add(row)
    await db.flush()
    await _apply_roles(db, row.id, payload.roles or ["editor"])
    db.add(
        AuditEvent(
            actor=actor.username,
            action="account.create",
            target_type="account",
            target_id=row.id,
            detail={"username": row.username},
        )
    )
    return await _serialize(db, row)


@router.put("/accounts/{account_id}")
async def update_account(
    account_id: str,
    payload: AccountUpdate,
    actor: Account = Depends(require_permission("admin.accounts")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(Account, account_id)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    if payload.displayName is not None:
        row.display_name = payload.displayName.strip() or row.username
    if payload.email is not None:
        row.email = payload.email or None
    if payload.password:
        row.password_hash = password_digest(payload.password)
    if payload.isActive is not None:
        if row.id == actor.id and not payload.isActive:
            raise HTTPException(status_code=400, detail="不能停用当前登录账号")
        row.is_active = payload.isActive
    if payload.isSuperuser is not None:
        if row.id == actor.id and not payload.isSuperuser:
            raise HTTPException(status_code=400, detail="不能取消自己的超级管理员身份")
        row.is_superuser = payload.isSuperuser
    if payload.roles is not None:
        await _apply_roles(db, row.id, payload.roles)
    db.add(
        AuditEvent(
            actor=actor.username,
            action="account.update",
            target_type="account",
            target_id=row.id,
            detail={key: value for key, value in payload.model_dump(exclude_unset=True).items() if key != "password"},
        )
    )
    await db.flush()
    return await _serialize(db, row)


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: str,
    actor: Account = Depends(require_permission("admin.accounts")),
    db: AsyncSession = Depends(get_db),
):
    if account_id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    row = await db.get(Account, account_id)
    if not row:
        raise HTTPException(status_code=404, detail="账号不存在")
    await db.execute(delete(AccountRole).where(AccountRole.account_id == account_id))
    await db.delete(row)
    db.add(
        AuditEvent(
            actor=actor.username,
            action="account.delete",
            target_type="account",
            target_id=account_id,
            detail={"username": row.username},
        )
    )
    return {"ok": True}


@router.get("/audit")
async def audit_tail(
    limit: int = 50,
    _: Account = Depends(require_permission("admin.accounts")),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(max(1, min(limit, 200)))
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": row.id,
                "actor": row.actor,
                "action": row.action,
                "targetType": row.target_type,
                "targetId": row.target_id,
                "detail": row.detail or {},
                "createdAt": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }
