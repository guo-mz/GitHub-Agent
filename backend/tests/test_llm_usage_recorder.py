"""记录器：成功 / 无 usage / 失败 / 流式 / failover 组。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from app.services.llm_usage import store as usage_store
from app.services.llm_usage.context import llm_call_context
from app.services.llm_usage.extract import UsageExtract
from app.services.llm_usage.recorder import ainvoke_tracked, astream_tracked, build_record


@pytest.fixture()
def json_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "llm_calls.json"
    monkeypatch.setattr(usage_store, "STORE_PATH", path)
    usage_store.reset_store_state_for_tests()

    async def _no_pg():
        usage_store._pg_ready = False  # noqa: SLF001
        return None

    monkeypatch.setattr(usage_store, "_get_engine", _no_pg)
    yield path


class FakeLLM:
    def __init__(self, resp=None, exc: Exception | None = None, chunks=None):
        self.resp = resp
        self.exc = exc
        self.chunks = chunks or []

    async def ainvoke(self, messages):
        if self.exc:
            raise self.exc
        return self.resp

    async def astream(self, messages):
        if self.exc:
            raise self.exc
        for c in self.chunks:
            yield c


def test_ainvoke_tracked_success_with_usage(json_store):
    resp = SimpleNamespace(
        content="ok",
        usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
        response_metadata={"model_name": "actual-qwen"},
    )
    llm = FakeLLM(resp=resp)

    async def _run():
        with llm_call_context(
            trace_id="t1",
            task_id="task1",
            thread_id="th1",
            module_name="coding",
            node_name="coding_agent",
        ):
            return await ainvoke_tracked(
                llm,
                [],
                provider="aliyun",
                requested_model="qwen-plus",
                label="coding_llm",
            )

    out = asyncio.run(_run())
    assert out is resp
    items = usage_store._json_load()  # noqa: SLF001
    assert len(items) == 1
    rec = items[0]
    assert rec["status"] == "ok"
    assert rec["requested_model"] == "qwen-plus"
    assert rec["actual_model"] == "actual-qwen"
    assert rec["input_tokens"] == 11
    assert rec["output_tokens"] == 3
    assert rec["usage_source"] == "provider_metadata"
    assert rec["task_id"] == "task1"
    assert rec["module_name"] == "coding"
    assert rec["latency_ms"] is not None


def test_ainvoke_missing_usage(json_store):
    resp = SimpleNamespace(content="x", usage_metadata=None, response_metadata={})

    async def _run():
        await ainvoke_tracked(
            FakeLLM(resp=resp),
            [],
            provider="aliyun",
            requested_model="qwen-plus",
            label="agent",
        )

    asyncio.run(_run())
    rec = usage_store._json_load()[0]  # noqa: SLF001
    assert rec["usage_source"] == "unavailable"
    assert rec["input_tokens"] is None
    assert rec["actual_model"] is None
    assert rec["estimated_cost"] is None


def test_ainvoke_timeout_recorded(json_store):
    llm = FakeLLM(exc=asyncio.TimeoutError())

    async def _run():
        await ainvoke_tracked(
            llm, [], provider="deepseek", requested_model="deepseek-chat", label="agent"
        )

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(_run())
    rec = usage_store._json_load()[0]  # noqa: SLF001
    assert rec["status"] == "timeout"
    assert rec["error_type"] == "TimeoutError"
    assert rec["input_tokens"] is None


def test_astream_ttft_and_usage(json_store):
    chunks = [
        SimpleNamespace(content="a", usage_metadata=None, response_metadata={}),
        SimpleNamespace(
            content="b",
            usage_metadata={"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
            response_metadata={"model_name": "stream-m"},
        ),
    ]

    async def _run():
        parts: List[Any] = []
        async for c in astream_tracked(
            FakeLLM(chunks=chunks),
            [],
            provider="aliyun",
            requested_model="qwen-plus",
            label="ai_edit_stream",
        ):
            parts.append(c)
        return parts

    parts = asyncio.run(_run())
    assert len(parts) == 2
    rec = usage_store._json_load()[0]  # noqa: SLF001
    assert rec["call_type"] == "stream"
    assert rec["time_to_first_token_ms"] is not None
    assert rec["actual_model"] == "stream-m"
    assert rec["total_tokens"] == 4


def test_failover_group_separate_attempts(json_store, monkeypatch):
    from app.agents import llm_utils as lu

    class P:
        def __init__(self, name, model):
            self.name = name
            self.model = model
            self.api_key = "k"
            self.base_url = "http://example"

    providers = [P("aliyun", "qwen-plus"), P("deepseek", "deepseek-chat")]
    monkeypatch.setattr(lu, "_providers", lambda: providers)

    calls = {"n": 0}

    class Flaky:
        async def ainvoke(self, messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("down")
            return SimpleNamespace(
                content="ok",
                usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                response_metadata={"model_name": "deepseek-chat"},
            )

    monkeypatch.setattr(lu, "make_chat_llm", lambda **kw: Flaky())

    from langchain_core.messages import HumanMessage

    async def _run():
        return await lu.ainvoke_llm([HumanMessage(content="hi")], label="agent", timeout=15)

    resp = asyncio.run(_run())
    assert resp.content == "ok"
    items = usage_store._json_load()  # noqa: SLF001
    assert len(items) == 2
    groups = {it["call_group_id"] for it in items}
    assert len(groups) == 1
    assert any(it["status"] == "ok" for it in items)
    assert any(it["status"] == "error" for it in items)


def test_build_record_never_fakes_actual_model():
    rec = build_record(
        provider="aliyun",
        requested_model="qwen-plus",
        call_type="chat",
        status="ok",
        started=0,
        finished=0.1,
        label="agent",
        actual_model=None,
        usage=UsageExtract(usage_source="unavailable"),
    )
    assert rec["actual_model"] is None
    assert rec["requested_model"] == "qwen-plus"
