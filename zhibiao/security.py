from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.database import get_db
from zhibiao.models import Account, AccountRole, Permission, RolePermission

COOKIE_NAME = "zb2_session"
SESSION_TTL = 7 * 24 * 3600


def password_digest(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def password_matches(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


@dataclass
class Session:
    token: str
    account_id: str
    created_at: float


class SessionStore:
    def __init__(self) -> None:
        self._items: dict[str, Session] = {}

    def _purge(self) -> None:
        cutoff = time.time() - SESSION_TTL
        for token in [t for t, s in self._items.items() if s.created_at < cutoff]:
            self._items.pop(token, None)

    def issue(self, account: Account) -> Session:
        self._purge()
        session = Session(token=secrets.token_hex(32), account_id=account.id, created_at=time.time())
        self._items[session.token] = session
        return session

    def lookup(self, token: str | None) -> Session | None:
        if not token:
            return None
        self._purge()
        return self._items.get(token)

    def revoke(self, token: str | None) -> None:
        if token:
            self._items.pop(token, None)


sessions = SessionStore()


def request_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME, "")


def current_token(request: Request) -> str:
    return request_token(request)


async def load_account(request: Request, db: AsyncSession = Depends(get_db)) -> Account:
    session = sessions.lookup(request_token(request))
    if not session:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    account = await db.get(Account, session.account_id)
    if not account or not account.is_active:
        sessions.revoke(session.token)
        raise HTTPException(status_code=401, detail="账号不可用")
    request.state.account_id = account.id
    return account


async def permissions_of(db: AsyncSession, account: Account) -> list[str]:
    if account.is_superuser:
        rows = (await db.execute(select(Permission.code))).scalars().all()
        return list(rows)
    role_codes = (
        await db.execute(select(AccountRole.role_code).where(AccountRole.account_id == account.id))
    ).scalars().all()
    if not role_codes:
        return []
    rows = (
        await db.execute(
            select(Permission.code).where(
                Permission.code.in_(
                    select(RolePermission.permission_code).where(
                        RolePermission.role_code.in_(list(role_codes))
                    )
                )
            )
        )
    ).scalars().all()
    return list(rows)


async def roles_of(db: AsyncSession, account: Account) -> list[str]:
    if account.is_superuser:
        return ["admin"]
    rows = (
        await db.execute(select(AccountRole.role_code).where(AccountRole.account_id == account.id))
    ).scalars().all()
    return list(rows)


def require_permission(code: str):
    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ) -> Account:
        account = await load_account(request, db)
        if account.is_superuser:
            return account
        allowed = await permissions_of(db, account)
        if code not in allowed:
            raise HTTPException(status_code=403, detail=f"缺少权限: {code}")
        return account

    return dependency
