from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

from openai import AsyncOpenAI
from sqlalchemy import select

from zhibiao.database import database_ready, session_factory
from zhibiao.models import AiProvider, Assistant
from zhibiao.settings import get_settings

logger = logging.getLogger(__name__)

_RETRYABLE = (429, 500, 502, 503, 504)


def _strip_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


def _temperature(model: str, value: float | None, fallback: float) -> float:
    if "kimi" in model.lower():
        return 1.0
    return fallback if value is None else value


class ChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 60,
        temperature: float = 0.6,
        max_tokens: int = 0,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = AsyncOpenAI(
            api_key=api_key or "not-configured",
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def _sleep_for(self, exc: Exception, attempt: int) -> None:
        wait = min(60, 5 * (2 ** min(attempt, 3)))
        response = getattr(exc, "response", None)
        if response is not None:
            header = response.headers.get("retry-after")
            if header and header.isdigit():
                wait = min(120, int(header))
        logger.warning("模型限流或暂不可用，%s 秒后重试: %s", wait, exc)
        await asyncio.sleep(wait)

    async def chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        attempt = 0
        while True:
            try:
                kwargs: dict = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": _temperature(self.model, temperature, self.temperature),
                }
                if max_tokens or self.max_tokens:
                    kwargs["max_tokens"] = max_tokens or self.max_tokens
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                response = await self._client.chat.completions.create(**kwargs)
                return response.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                if status == 429:
                    await self._sleep_for(exc, attempt)
                    attempt += 1
                    continue
                if status in _RETRYABLE and attempt < 2:
                    await self._sleep_for(exc, attempt)
                    attempt += 1
                    continue
                raise

    async def json_chat(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        text = await self.chat(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        try:
            payload = json.loads(_strip_json(text))
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise ValueError("模型未返回有效 JSON") from None
            payload = json.loads(match.group(0))
        if not isinstance(payload, dict):
            raise ValueError("模型返回的 JSON 不是对象")
        return payload

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        attempt = 0
        while True:
            started = False
            try:
                kwargs: dict = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": _temperature(self.model, temperature, self.temperature),
                    "stream": True,
                }
                if max_tokens or self.max_tokens:
                    kwargs["max_tokens"] = max_tokens or self.max_tokens
                stream = await self._client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        started = True
                        yield delta
                return
            except Exception as exc:  # noqa: BLE001
                status = getattr(exc, "status_code", None)
                if started:
                    raise
                if status == 429:
                    await self._sleep_for(exc, attempt)
                    attempt += 1
                    continue
                if status in _RETRYABLE and attempt < 2:
                    await self._sleep_for(exc, attempt)
                    attempt += 1
                    continue
                raise


async def _provider_settings(model: str | None = None) -> dict:
    settings = get_settings()
    data = {
        "base_url": settings.ai_base_url,
        "api_key": settings.ai_api_key,
        "model": model or settings.ai_model,
        "timeout": settings.ai_timeout,
    }
    if not database_ready():
        return data
    try:
        async with session_factory()() as db:
            provider = (
                await db.execute(
                    select(AiProvider).where(
                        AiProvider.is_active.is_(True), AiProvider.is_default.is_(True)
                    )
                )
            ).scalar_one_or_none()
            if provider and provider.api_key:
                data.update(
                    {
                        "base_url": provider.base_url,
                        "api_key": provider.api_key,
                        "model": model or provider.model or data["model"],
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取模型配置失败，使用环境配置: %s", exc)
    return data


async def _assistant_settings(key: str | None) -> dict | None:
    if not key or not database_ready():
        return None
    try:
        async with session_factory()() as db:
            assistant = (
                await db.execute(select(Assistant).where(Assistant.key == key))
            ).scalar_one_or_none()
            if not assistant:
                return None
            provider = None
            if assistant.provider_id:
                provider = await db.get(AiProvider, assistant.provider_id)
            if not provider or not provider.is_active:
                provider = (
                    await db.execute(
                        select(AiProvider).where(
                            AiProvider.is_active.is_(True), AiProvider.is_default.is_(True)
                        )
                    )
                ).scalar_one_or_none()
            return {
                "enabled": bool(assistant.enabled),
                "base_url": provider.base_url if provider else "",
                "api_key": provider.api_key if provider else "",
                "model": assistant.model or (provider.model if provider else ""),
                "temperature": assistant.temperature,
                "max_tokens": assistant.max_tokens,
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取协作助手配置失败，使用默认模型服务: %s", exc)
        return None


async def get_chat_client(
    model: str | None = None,
    assistant: str | None = None,
) -> ChatClient:
    data = await _provider_settings(model)
    template = await _assistant_settings(assistant)
    temperature = 0.6
    max_tokens = 0
    if template and template["enabled"]:
        if template["base_url"]:
            data["base_url"] = template["base_url"]
        if template["api_key"]:
            data["api_key"] = template["api_key"]
        data["model"] = model or template["model"] or data["model"]
        temperature = template["temperature"]
        max_tokens = template["max_tokens"]
    return ChatClient(
        data["base_url"],
        data["api_key"],
        data["model"],
        data["timeout"],
        temperature=temperature,
        max_tokens=max_tokens,
    )
