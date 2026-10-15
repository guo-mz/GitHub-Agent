"""Phase-1 安全边界：旁路写入、认证、哈希、测试门禁、持久化降级。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.agents.publish_agent import evaluate_test_gate, run_publish
from app.config import get_settings
from app.core import runtime_mode, security_auth
from app.services import approval_store, workspace_service as ws
from app.services.write_gate import GitHubWriteDenied, permit_github_write, require_github_write


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_store, "STORE_PATH", tmp_path / "approvals.json")
    monkeypatch.setattr(ws, "ROOT", tmp_path / "workspaces")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_write_gate_denies_without_permit():
    with pytest.raises(GitHubWriteDenied):
        require_github_write(operation="update_file", repo="o/r")


def test_write_gate_allows_with_permit():
    with permit_github_write(
        task_id="t1", reason="publish_agent", content_hash="abc", repo="o/r"
    ):
        p = require_github_write(operation="create_pr", repo="o/r")
        assert p.content_hash == "abc"


def test_content_hash_changes_on_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "ROOT", tmp_path / "workspaces")
    tid = "hash-tid"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "a.py", "old\n")
    ws.write_work_file(tid, "a.py", "new1\n")
    m1 = ws.build_publish_manifest(tid)
    h1 = m1["content_hash"]
    ws.freeze_approved_snapshot(tid)
    assert ws.load_approved_manifest(tid)["content_hash"] == h1

    ok, reason, _ = ws.verify_workspace_matches_approved(tid, h1)
    assert ok and reason == "ok"

    ws.write_work_file(tid, "a.py", "new2\n")
    ok2, reason2, cur = ws.verify_workspace_matches_approved(tid, h1)
    assert ok2 is False
    assert reason2 == "content_hash_mismatch"
    assert cur["content_hash"] != h1


def test_content_hash_fails_on_new_file():
    tid = "hash-add"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "a.py", "a\n")
    ws.write_work_file(tid, "a.py", "a2\n")
    h = ws.freeze_approved_snapshot(tid)["content_hash"]
    ws.write_work_file(tid, "b.py", "b\n")
    ok, reason, _ = ws.verify_workspace_matches_approved(tid, h)
    assert ok is False
    assert reason == "content_hash_mismatch"


def test_content_hash_fails_on_delete_path():
    tid = "hash-del"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "a.py", "a\n")
    ws.write_base_file(tid, "b.py", "b\n")
    ws.write_work_file(tid, "a.py", "a2\n")
    ws.write_work_file(tid, "b.py", "b2\n")
    h = ws.freeze_approved_snapshot(tid)["content_hash"]
    # 删除 work 中 b.py → 相对 base 变为 deleted，集合变化
    bp = ws._abs(ws._task_root(tid), "work", "b.py")
    bp.unlink()
    ok, reason, _ = ws.verify_workspace_matches_approved(tid, h)
    assert ok is False


def test_old_publish_approval_cannot_authorize_new_hash(monkeypatch):
    """批准哈希为 old；快照/工作区已变 → 即使三门齐开也拒绝（且无 GitHub 写）。"""
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    monkeypatch.setattr(
        "app.agents.publish_agent.evaluate_test_gate",
        lambda *_a, **_k: (True, "ok"),
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.ws.verify_and_load_approved_snapshot",
        lambda *_a, **_k: (False, "content_hash_mismatch", {}, {}),
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.ws.build_publish_manifest",
        lambda *_a, **_k: {"content_hash": "new"},
    )

    async def _inv(*_a, **_k):
        return None

    monkeypatch.setattr(
        "app.agents.publish_agent._invalidate_publish_approvals", _inv
    )
    env = asyncio.run(
        run_publish(
            task_id="t-old",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash="old",
            test_report={
                "passed": True,
                "not_run": False,
                "failed": False,
                "content_hash": "old",
            },
        )
    )
    assert env.status == "failed"
    assert "snapshot_integrity" in (env.error or "") or "content_hash" in (
        env.error or ""
    )


@pytest.mark.parametrize(
    "report",
    [
        {"status": "not_run", "not_run": True, "passed": False},
        {"status": "failed", "failed": True, "passed": False},
        {"status": "timeout"},
        {"status": "error"},
        {"unavailable": True, "passed": False},
        {"passed": False},
        {"passed": True, "not_run": False},  # 伪造 passed 也不得放行
    ],
)
def test_test_gate_blocks_non_passed(report):
    ok, reason = evaluate_test_gate(report)
    assert ok is False
    assert reason


def test_auth_fail_closed_without_token(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", "")
    monkeypatch.setenv("ALLOW_INSECURE_NO_AUTH", "false")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as ei:
        security_auth.verify_operator_token(action="test")
    assert ei.value.status_code == 401
    get_settings.cache_clear()


def test_auth_accepts_valid_token(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", "secret-op")
    monkeypatch.setenv("ALLOW_INSECURE_NO_AUTH", "false")
    get_settings.cache_clear()
    security_auth.verify_operator_token(x_operator_token="secret-op", action="test")
    with pytest.raises(HTTPException):
        security_auth.verify_operator_token(x_operator_token="wrong", action="test")
    get_settings.cache_clear()


def test_editor_save_and_token_require_auth_and_deny_write(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", "op-token")
    monkeypatch.setenv("ALLOW_INSECURE_NO_AUTH", "false")
    monkeypatch.setenv("ALLOW_EPHEMERAL_HITL", "true")
    get_settings.cache_clear()

    from app.main import app

    client = TestClient(app)

    # 未认证
    r0 = client.put(
        "/api/repos/o/r/file",
        json={"path": "a.py", "content": "x", "message": "m", "open_pr": True},
    )
    assert r0.status_code == 401

    r1 = client.post("/api/github/token", json={"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789", "repo": "o/r"})
    assert r1.status_code == 401

    r2 = client.post("/api/approval/tid-x", json={"decision": "approve"})
    assert r2.status_code == 401

    # 已认证但仍拒绝编辑器写
    headers = {"X-Operator-Token": "op-token"}
    r3 = client.put(
        "/api/repos/o/r/file",
        json={"path": "a.py", "content": "x", "message": "m", "open_pr": True},
        headers=headers,
    )
    assert r3.status_code == 403
    assert "禁用" in (r3.json().get("detail") or "")

    r4 = client.post(
        "/api/repos/o/r/bootstrap",
        headers=headers,
    )
    assert r4.status_code == 403

    get_settings.cache_clear()


def test_hitl_blocked_without_durable_checkpoint(monkeypatch):
    monkeypatch.setenv("ALLOW_EPHEMERAL_HITL", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(runtime_mode, "get_checkpointer_mode", lambda: "memory")
    monkeypatch.setattr(
        "app.core.runtime_mode.redis_service",
        type("R", (), {"fallback": True, "_client": None})(),
    )
    # re-import path uses get_checkpointer_mode from checkpointer via runtime_mode
    monkeypatch.setattr("app.core.runtime_mode.get_checkpointer_mode", lambda: "memory")
    with pytest.raises(HTTPException) as ei:
        runtime_mode.assert_hitl_task_allowed(action="chat_stream")
    assert ei.value.status_code == 503
    get_settings.cache_clear()


def test_github_client_write_denied_without_permit():
    from app.services.github_client import github_client

    with pytest.raises(GitHubWriteDenied):
        asyncio.run(github_client.create_branch("o", "r", "agent/x", from_branch="main"))
