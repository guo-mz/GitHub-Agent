"""MCP over HTTP JSON-RPC（调试）+ /health。

正式 Cursor 连接请用 stdio：`python -m app.mcp_server`。
本服务便于 curl 验收 tools/list，不依赖官方 mcp/SSE 包。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
for p in (str(_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.mcp_server.protocol import (  # noqa: E402
    SERVER_NAME,
    McpSession,
    list_tool_names,
    tools_list_payload,
)

app = FastAPI(title="GitHub Agent MCP", version="1.0.0")
_session = McpSession()


@app.get("/health")
async def health():
    settings = get_settings()
    return {
        "ok": True,
        "service": "mcp",
        "name": SERVER_NAME,
        "transport": "http-jsonrpc",
        "tools": list_tool_names(),
        "tool_count": len(list_tool_names()),
        "transformer_rerank": settings.transformer_rerank_enabled,
        "transformer_model": settings.transformer_rerank_model,
    }


@app.get("/tools")
async def tools():
    return {"tools": tools_list_payload()}


@app.post("/mcp")
async def mcp_rpc(request: Request):
    """单条 JSON-RPC POST（initialize / tools/list / tools/call）。"""
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid"}}, 400)
    resp = await _session.handle(body)
    if resp is None:
        return JSONResponse({"ok": True})
    return JSONResponse(resp)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "services.mcp_svc.main:app",
        host="127.0.0.1",
        port=8013,
        reload=False,
        app_dir=str(_ROOT),
    )


if __name__ == "__main__":
    main()
