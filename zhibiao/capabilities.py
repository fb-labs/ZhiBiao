"""模型服务里的能力开关：OCR 接口与标书生图，默认全部关闭。

配置保存在 `zb_app_settings` 的 `capabilities` 一行，结构：

    {
      "ocr":   {"enabled": false, "baseUrl": "", "apiKey": "", "model": "", "maxPages": 10},
      "image": {"enabled": false, "baseUrl": "", "apiKey": "", "model": "",
                "size": "1024x1024", "maxPerSection": 2, "maxPerBook": 12}
    }

开关关闭时调用方必须完全不产生外部请求，行为与升级前一致。
"""

from __future__ import annotations

import logging

from zhibiao.database import session_factory
from zhibiao.models import AppSetting

logger = logging.getLogger(__name__)

CAPABILITIES_KEY = "capabilities"

DEFAULT_CAPABILITIES: dict = {
    "ocr": {
        "enabled": False,
        "baseUrl": "",
        "apiKey": "",
        "model": "",
        "maxPages": 10,
    },
    "image": {
        "enabled": False,
        "baseUrl": "",
        "apiKey": "",
        "model": "",
        "size": "1024x1024",
        "maxPerSection": 2,
        "maxPerBook": 8,
    },
}


def _text(value, fallback: str = "", limit: int = 255) -> str:
    text = str(value if value is not None else "").strip()
    return text[:limit] or fallback


def _int(value, fallback: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def normalize_capabilities(raw: dict | None) -> dict:
    """把任意来源的配置收敛成固定结构；缺失字段一律回到默认关闭。"""
    data = raw if isinstance(raw, dict) else {}
    ocr_raw = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
    image_raw = data.get("image") if isinstance(data.get("image"), dict) else {}
    ocr = DEFAULT_CAPABILITIES["ocr"]
    image = DEFAULT_CAPABILITIES["image"]
    return {
        "ocr": {
            "enabled": bool(ocr_raw.get("enabled", ocr["enabled"])),
            "baseUrl": _text(ocr_raw.get("baseUrl"), ocr["baseUrl"]).rstrip("/"),
            "apiKey": _text(ocr_raw.get("apiKey")),
            "model": _text(ocr_raw.get("model"), "", 80),
            "maxPages": _int(ocr_raw.get("maxPages"), ocr["maxPages"], 1, 50),
        },
        "image": {
            "enabled": bool(image_raw.get("enabled", image["enabled"])),
            "baseUrl": _text(image_raw.get("baseUrl"), image["baseUrl"]).rstrip("/"),
            "apiKey": _text(image_raw.get("apiKey")),
            "model": _text(image_raw.get("model"), "", 80),
            "size": _text(image_raw.get("size"), image["size"], 20),
            "maxPerSection": _int(
                image_raw.get("maxPerSection"), image["maxPerSection"], 0, 8
            ),
            "maxPerBook": _int(image_raw.get("maxPerBook"), image["maxPerBook"], 0, 60),
        },
    }


async def load_capabilities(db) -> dict:
    """读取配置（带默认值）。传入 AsyncSession，便于管理接口与业务共用。"""
    row = await db.get(AppSetting, CAPABILITIES_KEY)
    return normalize_capabilities(row.value if row else None)


async def save_capabilities(db, raw: dict, actor: str = "") -> dict:
    """保存配置；只覆盖 OCR / 生图两段，不影响其它 AppSetting。"""
    data = normalize_capabilities(raw)
    row = await db.get(AppSetting, CAPABILITIES_KEY)
    if row is None:
        row = AppSetting(key=CAPABILITIES_KEY, value=data)
        db.add(row)
    else:
        row.value = data
    if actor:
        from zhibiao.models import AuditEvent

        db.add(
            AuditEvent(
                actor=actor,
                action="ai.capabilities.update",
                target_type="capability",
                target_id=CAPABILITIES_KEY,
                detail={
                    "ocr": data["ocr"]["enabled"],
                    "image": data["image"]["enabled"],
                },
            )
        )
    await db.flush()
    return data


async def read_capability(kind: str) -> dict:
    """业务侧读取单项能力；数据库不可用或读失败时按「关闭」处理。"""
    try:
        async with session_factory()() as db:
            data = await load_capabilities(db)
        return data.get(kind) or dict(DEFAULT_CAPABILITIES.get(kind, {}))
    except Exception as exc:  # noqa: BLE001 - 读配置失败不能拖垮上传/生成
        logger.debug("读取能力配置失败，按关闭处理: %s", exc)
        return dict(DEFAULT_CAPABILITIES.get(kind, {}))


def capability_ready(cfg: dict | None) -> bool:
    """能力真正可用：开关打开且填了接口地址。"""
    return bool(cfg and cfg.get("enabled") and str(cfg.get("baseUrl") or "").strip())
