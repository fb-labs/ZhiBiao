from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from zhibiao.llm import ChatClient, get_chat_client

logger = logging.getLogger(__name__)


async def _noop(_percent: int, _message: str) -> None:
    return None


@dataclass
class AgentContext:
    tender_id: str
    job_id: str
    account: str
    on_progress: Callable[[int, str], Awaitable[None]] = _noop

    async def progress(self, percent: int, message: str) -> None:
        await self.on_progress(percent, message)


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[[AgentContext, dict], Awaitable[dict]]


@dataclass
class AgentSpec:
    key: str
    name: str
    description: str
    system_prompt: str
    tools: list[ToolSpec] = field(default_factory=list)
    max_steps: int = 6
    assistant_key: str = ""

    def tool_prompt(self) -> str:
        lines = [
            f"- {tool.name}: {tool.description}" for tool in self.tools
        ]
        return "\n".join(lines) or "- （无工具）"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "maxSteps": self.max_steps,
            "assistant": self.assistant_key,
            "tools": [{"name": tool.name, "description": tool.description} for tool in self.tools],
        }


ACTION_SYSTEM = """你是 {name}。{description}

你可以使用以下工具：
{tools}

执行规则：
1. 每轮只输出一个 JSON 对象，不要输出任何解释性文字或 Markdown。
2. 调用工具：{{"thought":"下一步理由","action":"工具名","args":{{...}}}}
3. 结束任务：{{"thought":"完成理由","action":"finish","args":{{"result":{{...}}}}}}
4. 只能使用上面列出的工具；工具返回错误时换一种做法或结束并说明。
5. 不得编造项目事实；不确定的信息标注“未明确”。
"""


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:12000]


class AgentRunner:
    async def run(
        self,
        spec: AgentSpec,
        context: AgentContext,
        task: str,
        payload: dict | None = None,
        assistant: str | None = None,
    ) -> dict:
        client: ChatClient = await get_chat_client(assistant=assistant or spec.assistant_key)
        messages: list[dict] = [
            {
                "role": "system",
                "content": ACTION_SYSTEM.format(
                    name=spec.name,
                    description=spec.description,
                    tools=spec.tool_prompt(),
                )
                + "\n"
                + spec.system_prompt,
            },
            {
                "role": "user",
                "content": f"{task}\n\n输入参数：{_safe_json(payload or {})}",
            },
        ]
        trace: list[dict] = []
        final: dict = {}
        for step in range(1, spec.max_steps + 1):
            await context.progress(
                int((step - 1) / max(1, spec.max_steps) * 90),
                f"{spec.name} · 第 {step} 步",
            )
            try:
                decision = await client.json_chat(messages, max_tokens=6000)
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s 决策失败", spec.name)
                return {"output": {}, "trace": trace, "error": f"模型调用失败：{exc}"}

            action = str(decision.get("action") or decision.get("tool") or "finish").strip()
            args = decision.get("args") or decision.get("arguments") or {}
            if not isinstance(args, dict):
                args = {"value": args}
            trace.append(
                {
                    "step": step,
                    "thought": str(decision.get("thought") or "")[:800],
                    "action": action,
                    "args": args,
                }
            )
            if action in {"finish", "final", "done"}:
                final = decision.get("result") or args.get("result") or args
                break

            tool = next((item for item in spec.tools if item.name == action), None)
            if not tool:
                messages.append({"role": "assistant", "content": _safe_json(decision)})
                messages.append(
                    {
                        "role": "user",
                        "content": f"没有工具 {action}。可用工具：{', '.join(tool.name for tool in spec.tools)}",
                    }
                )
                continue
            try:
                result = await tool.handler(context, args)
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s 工具 %s 执行失败", spec.name, action)
                result = {"error": str(exc)}
            trace[-1]["result"] = _preview(result)
            messages.append({"role": "assistant", "content": _safe_json(decision)})
            messages.append({"role": "user", "content": f"工具 {action} 返回：{_safe_json(result)}"})
            final = result if result.get("__final__") else final
            if result.get("__final__"):
                break
        else:
            await context.progress(94, f"{spec.name} · 达到最大步数")
            return {"output": final, "trace": trace, "error": "达到最大步数，任务未收敛"}

        await context.progress(96, f"{spec.name} · 正在整理结果")
        return {"output": final, "trace": trace}


def _preview(value: Any) -> Any:
    text = _safe_json(value)
    return json.loads(text) if len(text) < 12000 else {"preview": text[:2000]}
