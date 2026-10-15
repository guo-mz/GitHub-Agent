"""轻量 MCP JSON-RPC（stdio / 内存），不依赖官方 mcp 包（避免与 FastAPI/pydantic 打架）。

实现 initialize / tools/list / tools/call / ping，足够 Cursor 连上演示。
默认仅暴露只读工具；远程写工具不注册且 call 时显式拒绝。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.agents.tool_sets import ANALYSIS_TOOLS, WRITE_TOOL_NAMES, is_write_tool
from app.tools import ALL_TOOLS

logger = logging.getLogger(__name__)

SERVER_NAME = "github-agent"
PROTOCOL_VERSION = "2024-11-05"

# 额外远程写别名：即使不在 WRITE_TOOL_NAMES 也不得暴露
_MCP_WRITE_BLOCKLIST = frozenset(WRITE_TOOL_NAMES) | {
    "create_pr",
    "merge_pr",
    "create_branch",
    "update_file",
    "create_or_update_file",
    "push",
    "publish",
}


def _mcp_readonly_tools() -> List[Any]:
    """MCP 默认工具面：ANALYSIS_TOOLS 且排除任何写工具。"""
    out: List[Any] = []
    seen: set[str] = set()
    for t in ANALYSIS_TOOLS:
        name = getattr(t, "name", "") or ""
        if not name or name in seen:
            continue
        if is_write_tool(name) or name in _MCP_WRITE_BLOCKLIST:
            continue
        seen.add(name)
        out.append(t)
    return out


JsonDict = Dict[str, Any]
Handler = Callable[[Optional[JsonDict]], Awaitable[Any]]


def list_tool_names() -> List[str]:
    return [t.name for t in _mcp_readonly_tools()]


def _input_schema(tool: Any) -> dict:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return {"type": "object", "properties": {}}
    raw = schema.model_json_schema()
    if raw.get("type") != "object" and "$ref" in raw:
        return {"type": "object", "properties": {}, "additionalProperties": True}
    raw.setdefault("type", "object")
    return raw


def tools_list_payload() -> List[dict]:
    return [
        {
            "name": t.name,
            "description": (t.description or t.name).strip(),
            "inputSchema": _input_schema(t),
        }
        for t in _mcp_readonly_tools()
    ]


async def call_tool(name: str, arguments: Optional[dict] = None) -> List[dict]:
    if is_write_tool(name) or name in _MCP_WRITE_BLOCKLIST:
        text = json.dumps(
            {
                "ok": False,
                "error": f"MCP 只读：拒绝远程写工具 {name}",
                "blocked_by": "mcp_readonly",
            },
            ensure_ascii=False,
        )
        return [{"type": "text", "text": text}]
    tool_map = {t.name: t for t in _mcp_readonly_tools()}
    # 兼容：底层 ALL_TOOLS 中的只读工具若未进 ANALYSIS 列表则仍允许
    if name not in tool_map:
        for t in ALL_TOOLS:
            n = getattr(t, "name", "") or ""
            if n == name and not is_write_tool(n) and n not in _MCP_WRITE_BLOCKLIST:
                tool_map[n] = t
                break
    tool = tool_map.get(name)
    if tool is None:
        text = json.dumps({"ok": False, "error": f"unknown tool: {name}"}, ensure_ascii=False)
        return [{"type": "text", "text": text}]
    try:
        result = await tool.ainvoke(arguments or {})
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP tool %s failed", name)
        result = json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False)
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, default=str)
    return [{"type": "text", "text": result}]


class McpSession:
    """无状态 JSON-RPC 分发（MCP 子集）。"""

    def __init__(self) -> None:
        self.initialized = False

    async def handle(self, message: JsonDict) -> Optional[JsonDict]:
        if "method" not in message:
            return None
        mid = message.get("id")
        method = message["method"]
        params = message.get("params") or {}

        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "notifications/cancelled":
            return None

        try:
            result = await self._dispatch(method, params if isinstance(params, dict) else {})
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32000, "message": str(exc)[:400]},
            }
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def _dispatch(self, method: str, params: JsonDict) -> Any:
        if method == "initialize":
            self.initialized = True
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools_list_payload()}
        if method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            content = await call_tool(name, args if isinstance(args, dict) else {})
            return {"content": content, "isError": False}
        raise ValueError(f"unsupported method: {method}")
