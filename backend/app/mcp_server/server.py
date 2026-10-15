"""MCP Server 门面：导出工具列表与 stdio 运行入口。"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List

from app.mcp_server.protocol import (
    SERVER_NAME,
    McpSession,
    _input_schema,
    list_tool_names,
    tools_list_payload,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SERVER_NAME",
    "McpSession",
    "build_mcp_server",
    "list_tool_names",
    "_input_schema",
    "run_stdio",
]


class build_mcp_server:  # noqa: N801 — 兼容测试里的「构造 server」语义
    """轻量占位：真正会话用 McpSession；保留 .name 供单测。"""

    def __init__(self, name: str = SERVER_NAME) -> None:
        self.name = name
        self.session = McpSession()


async def run_stdio() -> None:
    """Cursor 默认：每行一条 JSON-RPC（Content-Length 帧亦兼容简单行模式）。"""
    session = McpSession()
    loop_read = sys.stdin.buffer
    write = sys.stdout.buffer

    def _write_message(msg: Dict[str, Any]) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        write.write(header + body)
        write.flush()

    while True:
        # Content-Length framing (MCP stdio)
        headers: Dict[str, str] = {}
        while True:
            line = loop_read.readline()
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                break
            try:
                key, val = line.decode("ascii", errors="replace").split(":", 1)
                headers[key.strip().lower()] = val.strip()
            except ValueError:
                continue
        length = int(headers.get("content-length") or "0")
        if length <= 0:
            continue
        raw = loop_read.read(length)
        if not raw:
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("invalid mcp json")
            continue
        if not isinstance(message, dict):
            continue
        resp = await session.handle(message)
        if resp is not None:
            _write_message(resp)
