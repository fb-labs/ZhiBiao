from __future__ import annotations

import logging

from sqlalchemy import func, select

from zhibiao.database import session_factory
from zhibiao.domain import PERMISSION_CATALOG, ROLE_CATALOG, ROLE_PERMISSIONS
from zhibiao.models import (
    Account,
    AccountRole,
    AiProvider,
    Assistant,
    Permission,
    Role,
    RolePermission,
    Template,
)
from zhibiao.security import password_digest
from zhibiao.settings import get_settings

logger = logging.getLogger(__name__)

ASSISTANTS = (
    ("analyst", "招标解读助手", "提炼资格条件、评分办法与实质性要求", 0.3, 4000),
    ("outliner", "大纲规划助手", "把解读结果编排为可写作的章节结构", 0.4, 4000),
    ("writer", "标书撰写助手", "按章节意图撰写响应正文", 0.6, 10000),
    ("reviewer", "评标审核助手", "以评标视角检查完整性、响应性与合规性", 0.2, 4000),
    ("reviser", "修订助手", "按审查意见重写章节正文", 0.5, 8000),
)

DEFAULT_TEMPLATE_CONFIG = {
    "pageMargin": "2.5cm",
    "bodyFont": "宋体",
    "bodySize": 12,
    "lineSpacing": 1.5,
    "headingFont": "黑体",
    "coverTitleSize": 22,
}


async def ensure_catalog() -> None:
    async with session_factory()() as db:
        existing_permissions = {
            row[0] for row in (await db.execute(select(Permission.code))).all()
        }
        for code, name, group in PERMISSION_CATALOG:
            if code not in existing_permissions:
                db.add(Permission(code=code, name=name, group_name=group))

        existing_roles = {row[0] for row in (await db.execute(select(Role.code))).all()}
        for code, name, description in ROLE_CATALOG:
            if code not in existing_roles:
                db.add(Role(code=code, name=name, description=description))

        await db.flush()

        for role_code, permission_codes in ROLE_PERMISSIONS.items():
            for permission_code in permission_codes:
                pair = (
                    await db.execute(
                        select(RolePermission).where(
                            RolePermission.role_code == role_code,
                            RolePermission.permission_code == permission_code,
                        )
                    )
                ).scalar_one_or_none()
                if not pair:
                    db.add(RolePermission(role_code=role_code, permission_code=permission_code))
        await db.commit()


async def ensure_bootstrap_admin() -> None:
    settings = get_settings()
    async with session_factory()() as db:
        account = (
            await db.execute(select(Account).where(Account.username == settings.bootstrap_username))
        ).scalar_one_or_none()
        if account:
            return
        account = Account(
            username=settings.bootstrap_username,
            email=None,
            display_name="系统管理员",
            password_hash=password_digest(settings.bootstrap_password),
            is_active=True,
            is_superuser=True,
        )
        db.add(account)
        await db.flush()
        db.add(AccountRole(account_id=account.id, role_code="admin"))
        await db.commit()
        logger.info("已创建初始管理员账号: %s", settings.bootstrap_username)


async def ensure_assistants() -> None:
    async with session_factory()() as db:
        rows = {
            row.key: row
            for row in (await db.execute(select(Assistant))).scalars().all()
        }
        for key, label, purpose, temperature, tokens in ASSISTANTS:
            current = rows.get(key)
            if current is not None:
                # 只升不降：历史库里 writer/reviser 还是 4000，会直接卡住长篇正文。
                if current.max_tokens < tokens:
                    current.max_tokens = tokens
                continue
            db.add(
                Assistant(
                    key=key,
                    label=label,
                    purpose=purpose,
                    temperature=temperature,
                    max_tokens=tokens,
                    enabled=True,
                    provider_id=None,
                    model="",
                )
            )
        await db.commit()


async def ensure_default_provider() -> None:
    settings = get_settings()
    if not settings.ai_api_key:
        return
    async with session_factory()() as db:
        count = await db.scalar(select(func.count()).select_from(AiProvider))
        if count:
            return
        db.add(
            AiProvider(
                label="默认模型服务",
                base_url=settings.ai_base_url,
                api_key=settings.ai_api_key,
                model=settings.ai_model,
                temperature=0.6,
                max_tokens=10000,
                is_active=True,
                is_default=True,
            )
        )
        await db.commit()
        logger.info("已根据环境变量创建默认模型服务")


async def ensure_templates() -> None:
    async with session_factory()() as db:
        count = await db.scalar(select(func.count()).select_from(Template))
        if count:
            return
        db.add(
            Template(
                name="标准投标文件",
                description="A4 纵向、宋体小四、1.5 倍行距，适用于政府采购服务类项目",
                config=DEFAULT_TEMPLATE_CONFIG,
                is_default=True,
            )
        )
        await db.commit()


async def seed_all() -> None:
    await ensure_catalog()
    await ensure_bootstrap_admin()
    await ensure_assistants()
    await ensure_default_provider()
    await ensure_templates()
