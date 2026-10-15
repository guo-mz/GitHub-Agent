"""tools_node：计划未确认时硬拦写工具；混合 tool_calls 不得 UnboundLocalError。"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage

from app.agent import nodes


def _run(coro):
    return asyncio.run(coro)


def test_write_only_blocked_when_plan_unconfirmed():
    state = {
        "plan_confirmed": False,
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "update_file", "id": "1", "args": {"path": "README.md", "content": "x"}},
                    {"name": "create_pr", "id": "2", "args": {}},
                ],
            )
        ],
        "repo": "o/r",
        "trace_id": "t-write",
        "changed_files": [],
    }
    out = _run(nodes.tools_node(state))
    assert out.get("need_approval") in (None, False)
    rows = []
    for m in out.get("messages") or []:
        assert isinstance(m, ToolMessage)
        data = json.loads(m.content)
        rows.append(data)
        assert data.get("ok") is False
        assert data.get("blocked_by") == "plan_confirm"
    assert len(rows) == 2


def test_mixed_write_and_read_no_unboundlocal_and_blocks_write():
    """回归：局部 import get_settings 曾导致混合 tool_calls 时 UnboundLocalError。"""
    state = {
        "plan_confirmed": False,
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "update_file", "id": "1", "args": {"path": "README.md", "content": "x"}},
                    {"name": "get_file_content", "id": "3", "args": {"path": "README.md"}},
                ],
            )
        ],
        "repo": "o/r",
        "trace_id": "t-mix",
        "changed_files": [],
    }

    async def fake_invoke(fn, args, label=""):
        return json.dumps({"ok": True, "path": args.get("path"), "content": "hello"}, ensure_ascii=False)

    with patch.object(nodes, "_tool_invoke", new=AsyncMock(side_effect=fake_invoke)):
        out = _run(nodes.tools_node(state))

    by_name = {}
    for m in out.get("messages") or []:
        assert isinstance(m, ToolMessage)
        by_name[m.name] = json.loads(m.content) if str(m.content).startswith("{") else {"raw": m.content}

    assert by_name["update_file"]["blocked_by"] == "plan_confirm"
    assert by_name["update_file"]["ok"] is False
    # 只读工具应继续执行（mock 成功），且未触发发布审批
    assert by_name["get_file_content"].get("ok") is True
    assert out.get("need_approval") in (None, False)


def test_plan_confirmed_allows_bind_list_contains_update_file():
    from app.agents.tool_sets import tools_for_agent

    names = {getattr(t, "name", "") for t in tools_for_agent(plan_confirmed=True, allow_publish=False)}
    assert "update_file" in names
    blocked = {getattr(t, "name", "") for t in tools_for_agent(plan_confirmed=False, allow_publish=False)}
    assert "update_file" not in blocked
