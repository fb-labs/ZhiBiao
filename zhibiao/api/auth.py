from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from zhibiao.database import get_db
from zhibiao.models import Account
from zhibiao.security import (
    COOKIE_NAME,
    SESSION_TTL,
    current_token,
    load_account,
    password_digest,
    password_matches,
    permissions_of,
    roles_of,
    sessions,
)

router = APIRouter(prefix="/auth", tags=["v2-auth"])


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)


class PasswordPayload(BaseModel):
    password: str = Field(min_length=6, max_length=128)


def _profile(account: Account, roles: list[str], permissions: list[str]) -> dict:
    return {
        "id": account.id,
        "username": account.username,
        "email": account.email,
        "displayName": account.display_name,
        "isSuperuser": account.is_superuser,
        "roles": roles,
        "permissions": permissions,
    }


@router.post("/login")
async def login(
    payload: LoginPayload, response: Response, db: AsyncSession = Depends(get_db)
):
    account = (
        await db.execute(
            select(Account).where(
                or_(Account.username == payload.username, Account.email == payload.username)
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not account or not account.is_active or not password_matches(
        payload.password, account.password_hash
    ):
        raise HTTPException(status_code=401, detail="账号或密码错误")

    session = sessions.issue(account)
    response.set_cookie(
        COOKIE_NAME,
        session.token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {
        "token": session.token,
        "account": _profile(account, await roles_of(db, account), await permissions_of(db, account)),
    }


@router.get("/me")
async def me(account: Account = Depends(load_account), db: AsyncSession = Depends(get_db)):
    return _profile(account, await roles_of(db, account), await permissions_of(db, account))


@router.post("/logout")
async def logout(request_token_value: str = Depends(current_token)):
    sessions.revoke(request_token_value)
    response = Response()
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.put("/password")
async def change_password(
    payload: PasswordPayload,
    account: Account = Depends(load_account),
    db: AsyncSession = Depends(get_db),
    token: str = Depends(current_token),
):
    account.password_hash = password_digest(payload.password)
    sessions.revoke(token)
    return {"ok": True}
