"""MCP Server：工具列表与 JSON-RPC 分发。"""

from __future__ import annotations

import asyncio

from app.mcp_server.protocol import (
    McpSession,
    _input_schema,
    list_tool_names,
    tools_list_payload,
)
from app.mcp_server.server import build_mcp_server
from app.tools import ALL_TOOLS


def test_list_tool_names_covers_all_tools():
    names = list_tool_names()
    assert "search_codebase" in names or "get_file_content" in names
    # MCP 默认只读：不得暴露远程写工具
    for banned in ("create_branch", "update_file", "create_pr", "merge_pr"):
        assert banned not in names
    assert set(names).issubset({t.name for t in ALL_TOOLS})


def test_input_schema_is_object():
    tool = next(t for t in ALL_TOOLS if t.name == "search_codebase")
    schema = _input_schema(tool)
    assert schema.get("type") == "object"
    assert "properties" in schema


def test_build_mcp_server():
    server = build_mcp_server()
    assert server.name == "github-agent"


def test_mcp_initialize_and_tools_list():
    async def _run():
        session = McpSession()
        init = await session.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test"},
                },
            }
        )
        assert init["result"]["serverInfo"]["name"] == "github-agent"
        listed = await session.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        names = {t["name"] for t in listed["result"]["tools"]}
        assert "search_codebase" in names or "get_file_content" in names
        assert "create_pr" not in names
        assert len(tools_list_payload()) == len(list_tool_names())
        assert len(tools_list_payload()) < len(ALL_TOOLS)

    asyncio.run(_run())
