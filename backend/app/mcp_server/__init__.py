"""MCP Server：把 ALL_TOOLS 暴露给 Cursor / Claude 等 MCP 客户端。

面试点：同一套 LangChain 工具契约，第二种传输（stdio / SSE HTTP），
不引入官方 mcp 包（与现有 FastAPI/pydantic 栈解耦）。
"""

from app.mcp_server.protocol import SERVER_NAME, list_tool_names, tools_list_payload
from app.mcp_server.server import build_mcp_server, run_stdio

__all__ = [
    "SERVER_NAME",
    "build_mcp_server",
    "list_tool_names",
    "tools_list_payload",
    "run_stdio",
]
