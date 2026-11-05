from __future__ import annotations

import asyncio

from zhibiao.capabilities import normalize_capabilities
from zhibiao.core.image_slots import image_placeholder
from zhibiao.images import fill_image_slots, is_illustration_hint, load_image


def test_capabilities_default_off():
    data = normalize_capabilities(None)
    assert data["ocr"]["enabled"] is False
    assert data["image"]["enabled"] is False
    assert data["image"]["maxPerBook"] == 8


def test_capabilities_normalize_clamps_values():
    data = normalize_capabilities(
        {
            "ocr": {"enabled": True, "baseUrl": "https://example.invalid/v1/", "maxPages": 999},
            "image": {"maxPerSection": -3, "maxPerBook": 100},
        }
    )
    assert data["ocr"]["enabled"] is True
    assert data["ocr"]["baseUrl"] == "https://example.invalid/v1"
    assert data["ocr"]["maxPages"] == 50
    assert data["image"]["maxPerSection"] == 0
    assert data["image"]["maxPerBook"] == 60


def test_only_illustration_slots_are_generated():
    assert is_illustration_hint("系统架构图（整图完整截取，图内文字清晰可辨）")
    assert is_illustration_hint("实施流程图")
    assert not is_illustration_hint("营业执照副本扫描件（加盖投标人公章）")
    assert not is_illustration_hint("信誉查询网页截图（须截全网址）")


def test_fill_image_slots_replaces_only_illustrations(monkeypatch, tmp_path):
    from zhibiao import images as module

    monkeypatch.setattr(module, "image_root", lambda: tmp_path)

    async def fake_request(_prompt: str, _cfg: dict) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake-image"

    monkeypatch.setattr(module, "request_image", fake_request)

    body = (
        "## 技术方案\n\n"
        + image_placeholder("系统架构图")
        + "\n\n"
        + image_placeholder("营业执照")
    )
    result, count = asyncio.run(
        fill_image_slots(body, {"baseUrl": "https://example.invalid/v1"}, 2)
    )
    assert count == 1
    assert "![系统架构图](/api/images/" in result
    assert "{{图片占位:营业执照" in result
    image_id = result.split("/api/images/")[1].split(")")[0]
    assert load_image(image_id) == b"\x89PNG\r\n\x1a\nfake-image"


def test_fill_image_slots_respects_budget(monkeypatch, tmp_path):
    from zhibiao import images as module

    monkeypatch.setattr(module, "image_root", lambda: tmp_path)

    async def fake_request(_prompt: str, _cfg: dict) -> bytes:
        return b"\x89PNG\r\n\x1a\nfake-image"

    monkeypatch.setattr(module, "request_image", fake_request)

    body = image_placeholder("系统架构图") + "\n" + image_placeholder("实施流程图")
    result, count = asyncio.run(
        fill_image_slots(body, {"baseUrl": "https://example.invalid/v1"}, 1)
    )
    assert count == 1
    assert result.count("/api/images/") == 1
