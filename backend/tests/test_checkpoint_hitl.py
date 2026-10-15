"""Checkpoint / HITL 恢复：明确区分 Redis 持久化与 Memory 降级。

层级说明：
- A 类：不依赖 Redis（模式区分、HITL fail-closed、进程内 interrupt/resume）
- B 类：需要本机 Redis Stack（AsyncRedisSaver）；不可用则 skip，不得伪造成功
- 真实 OS 进程重启验证见 scripts/verify_hitl_restart.py（本文件不假装已做进程重启）
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import get_settings
from app.core import checkpointer as ckpt_mod
from app.core import runtime_mode
from app.services import redis_service as redis_mod


def _redis_url() -> str:
    return (os.environ.get("REDIS_URL") or get_settings().redis_url or "").strip()


async def _redis_ping_ok() -> bool:
    # 丢弃可能绑定到已关闭 event loop 的旧客户端，再强制重连
    redis_mod.redis_service._client = None
    redis_mod.redis_service.fallback = False
    await redis_mod.redis_service.connect(force=True)
    return (not redis_mod.redis_service.fallback) and await redis_mod.redis_service.ping()


async def _async_redis_saver_available() -> bool:
    if not await _redis_ping_ok():
        return False
    st = await ckpt_mod.probe_checkpointer(force_reset=True)
    return st.get("mode") == "redis"


@pytest.fixture(autouse=True)
def _reset_singletons():
    ckpt_mod.reset_checkpointer()
    yield
    ckpt_mod.reset_checkpointer()


# ---------- A: 模式区分与 fail-closed ----------


def test_checkpointer_status_never_claims_redis_when_memory():
    from langgraph.checkpoint.memory import MemorySaver

    ckpt_mod.reset_checkpointer()
    ckpt_mod._checkpointer = MemorySaver()
    ckpt_mod._checkpointer_mode = "memory"
    ckpt_mod._checkpointer_type_name = "langgraph.checkpoint.memory.InMemorySaver"
    ckpt_mod._checkpointer_init_error = "simulated"
    st = ckpt_mod.checkpointer_status()
    assert st["mode"] == "memory"
    assert st["durable"] is False
    assert "不能跨重启" in st["note"] or "进程内" in st["note"]


def test_runtime_status_hitl_blocked_in_memory_without_ephemeral(monkeypatch):
    monkeypatch.setenv("ALLOW_EPHEMERAL_HITL", "false")
    get_settings.cache_clear()
    ckpt_mod.reset_checkpointer()
    ckpt_mod._checkpointer_mode = "memory"
    monkeypatch.setattr(runtime_mode, "get_checkpointer_mode", lambda: "memory")
    monkeypatch.setattr(redis_mod.redis_service, "fallback", True)
    monkeypatch.setattr(redis_mod.redis_service, "_client", None)

    st = runtime_mode.runtime_persistence_status()
    assert st["durable_checkpoint"] is False
    assert st["hitl_resume_guaranteed"] is False
    assert st["new_hitl_tasks_allowed"] is False

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        runtime_mode.assert_hitl_task_allowed(action="test")
    assert ei.value.status_code == 503
    get_settings.cache_clear()


def test_memory_mode_not_labeled_as_redis_persistence():
    ckpt_mod.reset_checkpointer()
    ckpt_mod._checkpointer_mode = "memory"
    ckpt_mod._checkpointer_type_name = "InMemorySaver"
    out = ckpt_mod.checkpointer_status()
    assert out["mode"] != "redis"
    assert out["durable"] is False


# ---------- A: 进程内 interrupt / 同 thread_id resume（MemorySaver）----------


class _HitlState(TypedDict):
    phase: str
    decision: str
    value: str


def _build_mini_hitl_graph(checkpointer: Any):
    def before(state: _HitlState) -> Dict[str, Any]:
        return {"phase": "before", "value": state.get("value") or "v1"}

    def wait(state: _HitlState) -> Dict[str, Any]:
        decision = interrupt({"type": "need_approval", "value": state.get("value")})
        if isinstance(decision, str):
            decision = {"decision": decision}
        dec = (decision or {}).get("decision") or "reject"
        return {"phase": "resumed", "decision": dec}

    def after(state: _HitlState) -> Dict[str, Any]:
        return {"phase": "done", "value": f"{state.get('value')}:{state.get('decision')}"}

    g = StateGraph(_HitlState)
    g.add_node("before", before)
    g.add_node("wait", wait)
    g.add_node("after", after)
    g.add_edge(START, "before")
    g.add_edge("before", "wait")
    g.add_edge("wait", "after")
    g.add_edge("after", END)
    return g.compile(checkpointer=checkpointer)


def test_memory_interrupt_pause_and_resume_same_thread_id():
    """进程内：interrupt → 新图实例共享 MemorySaver → 同 thread_id resume。

    证明 thread_id + Command(resume)；不等于 OS 进程重启验证。
    """

    async def _run():
        from langgraph.checkpoint.memory import MemorySaver

        saver = MemorySaver()
        thread_id = f"mem-hitl-{uuid.uuid4().hex[:12]}"
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10}

        g1 = _build_mini_hitl_graph(saver)
        await g1.ainvoke({"phase": "", "decision": "", "value": "alpha"}, config=config)
        st1 = await g1.aget_state(config)
        assert st1.next
        assert "wait" in (st1.next or ())

        g2 = _build_mini_hitl_graph(saver)
        st2 = await g2.aget_state(config)
        assert st2.next
        out = await g2.ainvoke(Command(resume={"decision": "approve"}), config=config)
        assert out.get("phase") == "done"
        assert out.get("decision") == "approve"
        assert "alpha" in (out.get("value") or "")
        assert not (await g2.aget_state(config)).next

    asyncio.run(_run())


def test_memory_checkpoint_not_visible_to_other_saver_instance():
    """不同 MemorySaver 实例不共享状态 → 内存模式不能跨进程。"""

    async def _run():
        from langgraph.checkpoint.memory import MemorySaver

        s1, s2 = MemorySaver(), MemorySaver()
        thread_id = f"mem-iso-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": thread_id}}
        g1 = _build_mini_hitl_graph(s1)
        await g1.ainvoke({"phase": "", "decision": "", "value": "x"}, config=config)
        assert (await g1.aget_state(config)).next
        g2 = _build_mini_hitl_graph(s2)
        st = await g2.aget_state(config)
        assert not st.next
        assert not (st.values or {})

    asyncio.run(_run())


# ---------- B: Redis / AsyncRedisSaver ----------


def test_redis_service_connects_when_available():
    async def _run():
        return await _redis_ping_ok()

    ok = asyncio.run(_run())
    if not ok:
        pytest.skip("Redis 不可用：持久化验证未完成（请 docker compose up -d redis）")
    assert redis_mod.redis_service.fallback is False


def test_async_redis_saver_mode_when_stack_available():
    async def _run():
        if not await _redis_ping_ok():
            return None
        return await ckpt_mod.probe_checkpointer(force_reset=True)

    st = asyncio.run(_run())
    if st is None:
        pytest.skip("Redis 不可用：无法验证 AsyncRedisSaver")
    if st.get("mode") != "redis":
        pytest.skip(
            f"AsyncRedisSaver 未就绪: mode={st.get('mode')} err={st.get('init_error')}"
        )
    assert st["durable"] is True
    assert st["mode"] == "redis"
    assert st["mode"] != "memory"


def test_redis_interrupt_save_and_resume_new_graph_instance():
    """Redis：interrupt 后新 AsyncRedisSaver + 同 thread_id resume。

    「重新创建图实例」级验证，不是 OS 进程重启。
    """

    async def _run():
        if not await _async_redis_saver_available():
            return {"skip": "AsyncRedisSaver 不可用"}

        from langgraph.checkpoint.redis.aio import AsyncRedisSaver

        url = _redis_url()
        thread_id = f"redis-hitl-{uuid.uuid4().hex[:12]}"
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10}

        saver1 = AsyncRedisSaver(redis_url=url)
        await saver1.asetup()
        g1 = _build_mini_hitl_graph(saver1)
        await g1.ainvoke({"phase": "", "decision": "", "value": "beta"}, config=config)
        st1 = await g1.aget_state(config)
        assert st1.next
        assert "wait" in (st1.next or ())

        saver2 = AsyncRedisSaver(redis_url=url)
        await saver2.asetup()
        g2 = _build_mini_hitl_graph(saver2)
        st2 = await g2.aget_state(config)
        assert st2.next, "Redis checkpoint must survive new graph instance"
        out = await g2.ainvoke(Command(resume={"decision": "approve"}), config=config)
        assert out.get("phase") == "done"
        assert out.get("decision") == "approve"
        assert "beta" in (out.get("value") or "")
        return {"ok": True, "thread_id": thread_id}

    result = asyncio.run(_run())
    if result.get("skip"):
        pytest.skip(result["skip"])
    assert result.get("ok")


def test_probe_reports_init_error_on_bad_redis_url(monkeypatch):
    """错误 URL 时必须降级 memory 并记录错误，不得声称 redis。"""

    async def _run():
        return await ckpt_mod.probe_checkpointer(force_reset=True)

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    get_settings.cache_clear()
    ckpt_mod.reset_checkpointer()
    st = asyncio.run(_run())
    assert st["mode"] == "memory"
    assert st["durable"] is False
    assert st.get("init_error")
    get_settings.cache_clear()
    ckpt_mod.reset_checkpointer()
