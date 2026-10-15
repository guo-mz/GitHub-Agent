"""查询 API：筛选、分页、Operator 权限、汇总。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services.llm_usage import store as usage_store
from app.services.llm_usage.extract import UsageExtract
from app.services.llm_usage.recorder import build_record, record_llm_attempt


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", "usage-secret")
    monkeypatch.setenv("ALLOW_INSECURE_NO_AUTH", "false")
    get_settings.cache_clear()

    path = tmp_path / "llm_calls.json"
    monkeypatch.setattr(usage_store, "STORE_PATH", path)
    usage_store.reset_store_state_for_tests()

    async def _no_pg():
        usage_store._pg_ready = False  # noqa: SLF001
        return None

    monkeypatch.setattr(usage_store, "_get_engine", _no_pg)

    from app.main import app

    yield TestClient(app)
    get_settings.cache_clear()


def _seed():
    async def _inner():
        u = UsageExtract(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            usage_source="provider_metadata",
        )
        rec = build_record(
            provider="aliyun",
            requested_model="qwen-plus",
            call_type="chat",
            status="ok",
            started=1.0,
            finished=1.2,
            label="coding_llm",
            actual_model="qwen-plus",
            usage=u,
            call_group_id="g1",
        )
        rec["task_id"] = "task-abc"
        rec["module_name"] = "coding"
        await record_llm_attempt(rec)

        fail = build_record(
            provider="deepseek",
            requested_model="deepseek-chat",
            call_type="chat",
            status="timeout",
            started=2.0,
            finished=2.5,
            label="agent",
            usage=UsageExtract(),
            error_type="TimeoutError",
            call_group_id="g2",
        )
        fail["task_id"] = "task-abc"
        await record_llm_attempt(fail)

    asyncio.run(_inner())


def test_unauthorized_cannot_read(client):
    _seed()
    r = client.get("/api/llm-usage/summary")
    assert r.status_code == 401


def test_summary_and_filters(client):
    _seed()
    h = {"X-Operator-Token": "usage-secret"}
    s = client.get("/api/llm-usage/summary", headers=h)
    assert s.status_code == 200
    body = s.json()
    assert body["ok"] is True
    assert body["call_count"] == 2
    assert body["storage_backend"] == "json"

    calls = client.get(
        "/api/llm-usage/calls",
        headers=h,
        params={"module_name": "coding", "limit": 10},
    )
    assert calls.status_code == 200
    data = calls.json()
    assert data["total"] >= 1
    assert all(i.get("module_name") == "coding" for i in data["items"])

    by_m = client.get("/api/llm-usage/by-module", headers=h).json()
    assert any(i["module_name"] == "coding" for i in by_m["items"])

    task = client.get("/api/llm-usage/tasks/task-abc", headers=h)
    assert task.status_code == 200
    td = task.json()
    assert td["call_count"] == 2
    assert "model_latency_sum_ms" in td
    assert "latency_note" in td

    fails = client.get("/api/llm-usage/failures", headers=h).json()
    assert fails["total"] >= 1


def test_pagination(client):
    _seed()
    h = {"X-Operator-Token": "usage-secret"}
    page = client.get(
        "/api/llm-usage/calls",
        headers=h,
        params={"offset": 0, "limit": 1},
    ).json()
    assert page["limit"] == 1
    assert len(page["items"]) == 1
    assert page["total"] == 2


def test_dedup_insert(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_store, "STORE_PATH", tmp_path / "llm_calls.json")
    usage_store.reset_store_state_for_tests()

    async def _no_pg():
        usage_store._pg_ready = False  # noqa: SLF001
        return None

    monkeypatch.setattr(usage_store, "_get_engine", _no_pg)

    async def _run():
        rec = {
            "id": "dup-1",
            "status": "ok",
            "created_at": "2026-04-01T00:00:00+00:00",
            "module_name": "coding",
        }
        await usage_store.insert_record(rec)
        await usage_store.insert_record(dict(rec))

    asyncio.run(_run())
    items = usage_store._json_load()  # noqa: SLF001
    assert sum(1 for i in items if i.get("id") == "dup-1") == 1
