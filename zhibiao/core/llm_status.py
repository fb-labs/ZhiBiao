"""大模型「是否可用」的统一判定：本地模型（Ollama / LM Studio / vLLM）没有 API Key。

线上问题：Ollama 这类本地推理服务不需要 API Key，但全平台 8 处入口都写成
`bool(llm.get("api_key")) and bool(llm.get("enabled"))`，于是：
* 生成正文、招标解读、合规检查、Agent 规划全部退回「未配置大模型」的规则模式；
* 「测试连接」直接返回「尚未配置 API Key」，连一次请求都不发。

改成：有密钥就一定算可用；没有密钥时，**只有指向本机/内网/Ollama 端口**才放行。
这样既支持本地模型，也不会因为忘填密钥就去打公网接口（那样只会拿到 401）。
"""

from __future__ import annotations

import re
from typing import Any

# 明确代表"本机/本容器宿主"的主机名
_LOCAL_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
    "host.docker.internal", "host.containers.internal", "gateway.docker.internal",
})
# Ollama 默认端口与常见本地 OpenAI 兼容端口
_KEYLESS_PORT_HINTS = ("11434", "1234", "8000", "8080")


def is_keyless_endpoint(base_url: str) -> bool:
    """该地址是否为无需密钥的本地/内网服务。"""
    url = str(base_url or "").strip().lower()
    if not url:
        return False
    if "ollama" in url or "lmstudio" in url or "vllm" in url:
        return True
    authority = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url).split("/", 1)[0]
    host, _, port = authority.rpartition(":") if ":" in authority else (authority, "", "")
    if not host:  # 没有端口的形式
        host, port = authority, ""
    host = host.strip("[]")
    if host in _LOCAL_HOSTS:
        return True
    match = re.match(r"^(\d{1,3})\.(\d{1,3})\.", host)
    if match:
        a, b = int(match.group(1)), int(match.group(2))
        # 10/8、172.16/12、192.168/16 都是内网，不可能是公网付费接口
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return True
    return port in _KEYLESS_PORT_HINTS


def llm_ready(llm: dict[str, Any] | None) -> bool:
    """配置是否可真正调用：启用 + （有密钥 或 指向本地/内网服务）。"""
    if not isinstance(llm, dict) or not llm.get("enabled"):
        return False
    if str(llm.get("api_key") or "").strip():
        return True
    return is_keyless_endpoint(str(llm.get("base_url") or ""))