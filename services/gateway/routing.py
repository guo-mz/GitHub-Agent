"""Gateway 路径分流 —— 纯函数，便于单测与面试讲解。"""

from __future__ import annotations

from typing import Literal, Optional

Upstream = Literal["agent", "repo"]

# 前缀匹配（长前缀优先）
_AGENT_PREFIXES = (
    "/api/chat",
    "/api/approval",
    "/api/eval",
    "/api/llm-usage",
)

_REPO_PREFIXES = (
    "/api/repos",
    "/api/auth",
    "/api/webhook",
    "/api/github",
)


def resolve_upstream(path: str) -> Optional[Upstream]:
    """根据请求 path 决定上游；/health 由 Gateway 自处理，返回 None。"""
    if not path:
        return None
    p = path if path.startswith("/") else f"/{path}"
    # 精确 health 不反代
    if p == "/health" or p.startswith("/health?"):
        return None
    for prefix in _AGENT_PREFIXES:
        if p == prefix or p.startswith(prefix + "/"):
            return "agent"
    for prefix in _REPO_PREFIXES:
        if p == prefix or p.startswith(prefix + "/"):
            return "repo"
    # OpenAPI 文档可落到 repo（只读信息）；默认 agent 也可
    if p in {"/docs", "/openapi.json", "/redoc"} or p.startswith("/docs/"):
        return "agent"
    return None
