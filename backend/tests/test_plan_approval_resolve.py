"""计划审批拒绝后必须关闭 pending，且不可用旧审批再次 resume。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.models.schemas import ApprovalDecision, ApprovalRequest
from app.services.redis_service import redis_service


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_memory(tmp_path, monkeypatch):
    """确保走内存 fallback，并清空审批键 + 磁盘 store。"""
    from app.services import approval_store

    monkeypatch.setattr(approval_store, "STORE_PATH", tmp_path / "approvals.json")
    approval_store.clear_all_for_tests()

    async def _prep():
        await redis_service.connect()
        redis_service.fallback = True
        mem = redis_service._memory
        for k in list(mem.kv.keys()):
            if k.startswith("approval:"):
                mem.delete(k)
        mem.sets.pop("approval:pending", None)

    _run(_prep())
    yield


def test_resolve_approval_clears_pending_and_marks_resolved():
    tid = "plan-reject-tid-1"

    async def _flow():
        await redis_service.save_pending_approval(
            tid,
            {
                "thread_id": tid,
                "approval_kind": "plan_confirm",
                "operation": "plan_confirm",
                "status": "pending",
            },
        )
        assert await redis_service.get_pending_approval(tid)
        await redis_service.resolve_approval(
            tid, decision="reject", approval_kind="plan_confirm"
        )
        assert await redis_service.get_pending_approval(tid) is None
        resolved = await redis_service.get_resolved_approval(tid)
        assert resolved is not None
        assert resolved["decision"] == "reject"
        assert resolved["approval_kind"] == "plan_confirm"
        pending_list = await redis_service.list_pending_approvals()
        assert not any(p.get("thread_id") == tid for p in pending_list)

    _run(_flow())


def test_submit_approval_rejects_reuse_after_resolve(monkeypatch):
    """API：resolved 后再次提交应 409，不能触发写操作路径。"""
    from app.api import approval as approval_api

    tid = "plan-reject-tid-2"

    async def _prep():
        await redis_service.connect()
        redis_service.fallback = True
        await redis_service.save_pending_approval(
            tid,
            {
                "thread_id": tid,
                "task_id": tid,
                "approval_kind": "plan_confirm",
                "operation": "plan_confirm",
                "status": "pending",
            },
        )
        await redis_service.resolve_approval(
            tid, decision="reject", approval_kind="plan_confirm"
        )

    _run(_prep())

    class _Req:
        async def is_disconnected(self):
            return False

    body = ApprovalRequest(
        decision=ApprovalDecision.APPROVE,
        comment="reuse",
        approval_kind="plan_confirm",
        task_id=tid,
    )

    async def _call():
        with pytest.raises(HTTPException) as ei:
            await approval_api.submit_approval(tid, body, _Req())
        assert ei.value.status_code == 409
        assert "already resolved" in str(ei.value.detail)

    _run(_call())


def test_await_plan_confirm_reject_resolves_pending(monkeypatch):
    """节点：拒绝计划后 pending 关闭。"""
    from app.agent import nodes

    tid = "plan-reject-tid-3"

    def _fake_interrupt(_payload):
        return {"decision": "reject", "comment": "no"}

    async def _flow():
        await redis_service.connect()
        redis_service.fallback = True
        monkeypatch.setattr(nodes, "interrupt", _fake_interrupt)
        state = {
            "trace_id": tid,
            "repo": "o/r",
            "plan_document": {"title": "t", "files_to_change": ["a.py"]},
            "analysis_report": {},
        }
        out = await nodes.await_plan_confirm_node(state)
        assert out["plan_confirmed"] is False
        assert out["phase"] == "cancelled"
        assert await redis_service.get_pending_approval(tid) is None
        resolved = await redis_service.get_resolved_approval(tid)
        assert resolved and resolved["decision"] == "reject"

    _run(_flow())
