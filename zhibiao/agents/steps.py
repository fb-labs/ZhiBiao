"""按步骤执行 Agent：四个流程步骤都以 Agent 为执行者，API 只是入口。

设计约束：
- 每个步骤的 Agent 工具直接调用 `zhibiao.pipeline_steps` 里的同一份实现，
  因此"点按钮"和"Agent 执行"走的是同一段业务逻辑，不会出现两套结果。
- Agent 失败（模型不可用、步数耗尽、没产出）时由调用方回落到同一份实现，
  保证用户流程永远有结果。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from zhibiao.agents.base import AgentContext, AgentRunner, AgentSpec
from zhibiao.agents.catalog import AGENTS
from zhibiao.agents.tools import TOOLS

logger = logging.getLogger(__name__)


async def _noop_progress(_percent: int, _message: str) -> None:
    return None


async def _prepare_payload(key: str, context: AgentContext, payload: dict[str, Any]) -> dict[str, Any]:
    """招标解析先把压缩摘要塞进输入，模型就不用反复读原文。"""
    if key != "tender_reader" or payload.get("tenderDigest"):
        return payload
    try:
        digest = await TOOLS["tender_digest"].handler(context, {})
    except Exception as exc:  # noqa: BLE001 - 摘要失败不影响后续工具读原文
        logger.warning("招标摘要准备失败：%s", exc)
        return payload
    if digest.get("digest"):
        return {**payload, "tenderDigest": digest["digest"]}
    return payload


async def run_step_agent(
    key: str,
    *,
    tender_id: str,
    task: str,
    payload: dict[str, Any] | None = None,
    account: str = "agent",
    job_id: str = "",
    on_progress: Callable[[int, str], Awaitable[None]] | None = None,
    spec: AgentSpec | None = None,
) -> dict[str, Any]:
    """执行一个步骤 Agent，返回 {output, trace, error}。"""
    agent = spec or AGENTS.get(key)
    if not agent:
        return {"output": {}, "trace": [], "error": f"未知 Agent：{key}"}
    context = AgentContext(
        tender_id=tender_id,
        job_id=job_id or f"step:{key}",
        account=account or "agent",
        on_progress=on_progress or _noop_progress,
    )
    body = await _prepare_payload(key, context, dict(payload or {}))
    try:
        return await AgentRunner().run(agent, context, task, body, assistant=agent.assistant_key)
    except RuntimeError as exc:
        if str(exc) == "__cancelled__":
            return {"output": {}, "trace": [], "error": "用户取消"}
        logger.exception("%s 执行失败", agent.name)
        return {"output": {}, "trace": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 任何异常都交给调用方兜底
        logger.exception("%s 执行失败", agent.name)
        return {"output": {}, "trace": [], "error": str(exc)}
