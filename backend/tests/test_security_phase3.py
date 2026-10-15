"""Phase-3 安全补漏：快照字节完整性、敏感读认证、测试门禁、写工具/Webhook。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agents.publish_agent import evaluate_test_gate, run_publish
from app.config import get_settings
from app.services import approval_store, workspace_service as ws


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_store, "STORE_PATH", tmp_path / "approvals.json")
    monkeypatch.setattr(ws, "ROOT", tmp_path / "workspaces")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _prep_snapshot(tid: str = "p3-snap") -> str:
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "a.py", "old\n")
    ws.write_work_file(tid, "a.py", "new\n")
    return ws.freeze_approved_snapshot(tid)["content_hash"]


def _gh_write_counters(monkeypatch):
    calls = {"create_branch": 0, "update_file": 0, "create_pr": 0}

    async def _cb(*_a, **_k):
        calls["create_branch"] += 1
        raise AssertionError("create_branch must not be called")

    async def _uf(*_a, **_k):
        calls["update_file"] += 1
        raise AssertionError("update_file must not be called")

    async def _pr(*_a, **_k):
        calls["create_pr"] += 1
        raise AssertionError("create_pr must not be called")

    monkeypatch.setattr(
        "app.agents.publish_agent.github_client.create_branch", _cb
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.github_client.update_file", _uf
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.github_client.get_file_content",
        AsyncMock(side_effect=Exception("no")),
    )

    fake_tool = MagicMock()
    fake_tool.ainvoke = AsyncMock(side_effect=_pr)
    monkeypatch.setattr(
        "app.tools.github_tools.create_pr", fake_tool, raising=False
    )
    return calls


# ----- P1: snapshot integrity -----


def test_snapshot_byte_tamper_fails_verify():
    tid = "tamper-bytes"
    h = _prep_snapshot(tid)
    path = ws._abs(ws._task_root(tid), "approved_snapshot", "a.py")
    path.write_bytes(b"EVIL\n")
    ok, reason, loaded, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False
    assert "file_hash_mismatch" in reason
    assert loaded == {}


def test_snapshot_file_deleted_fails():
    tid = "snap-del"
    h = _prep_snapshot(tid)
    path = ws._abs(ws._task_root(tid), "approved_snapshot", "a.py")
    path.unlink()
    ok, reason, _, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False
    assert "snapshot_missing" in reason or "missing" in reason


def test_snapshot_extra_file_fails():
    tid = "snap-extra"
    h = _prep_snapshot(tid)
    extra = ws._abs(ws._task_root(tid), "approved_snapshot", "evil.py")
    extra.write_bytes(b"x\n")
    ok, reason, _, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False
    assert "snapshot_extra_files" in reason


def test_manifest_tampered_fails():
    tid = "manif-tamper"
    h = _prep_snapshot(tid)
    mp = ws.approved_snapshot_dir(tid) / "MANIFEST.json"
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["content_hash"] = "0" * 64
    mp.write_text(json.dumps(data), encoding="utf-8")
    ok, reason, _, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False
    assert reason == "manifest_hash_mismatch"


def test_manifest_missing_fails():
    tid = "manif-miss"
    h = _prep_snapshot(tid)
    (ws.approved_snapshot_dir(tid) / "MANIFEST.json").unlink()
    ok, reason, _, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False
    assert "manifest_missing" in reason


def test_symlink_in_snapshot_fails(tmp_path):
    tid = "snap-link"
    h = _prep_snapshot(tid)
    link = ws.approved_snapshot_dir(tid) / "link.py"
    target = tmp_path / "outside.py"
    target.write_text("x", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink not permitted on this host")
    ok, reason, _, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False
    assert "symlink" in reason or "extra" in reason


def test_work_unchanged_but_snapshot_modified_fails_publish(monkeypatch):
    calls = _gh_write_counters(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    tid = "work-ok-snap-bad"
    h = _prep_snapshot(tid)
    # work 保持与冻结时一致；只改 snapshot
    path = ws._abs(ws._task_root(tid), "approved_snapshot", "a.py")
    path.write_bytes(b"tampered\n")
    env = asyncio.run(
        run_publish(
            task_id=tid,
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash=h,
            test_report={
                "passed": True,
                "not_run": False,
                "content_hash": h,
            },
        )
    )
    # 测试门禁先拦；即使绕过也应因快照失败且写调用为 0
    assert env.status == "failed"
    assert calls["create_branch"] == 0
    assert calls["update_file"] == 0
    assert calls["create_pr"] == 0


def test_snapshot_integrity_failure_zero_github_writes(monkeypatch):
    """绕过测试门禁后，快照篡改仍不得触发 GitHub 写。"""
    calls = _gh_write_counters(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    monkeypatch.setattr(
        "app.agents.publish_agent.evaluate_test_gate",
        lambda *_a, **_k: (True, "ok"),
    )
    tid = "integ-zero"
    h = _prep_snapshot(tid)
    path = ws._abs(ws._task_root(tid), "approved_snapshot", "a.py")
    path.write_bytes(b"bad\n")
    env = asyncio.run(
        run_publish(
            task_id=tid,
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash=h,
            test_report={"passed": True, "content_hash": h},
        )
    )
    assert env.status == "failed"
    assert "snapshot_integrity" in (env.error or "")
    assert calls["create_branch"] == 0
    assert calls["update_file"] == 0
    assert calls["create_pr"] == 0


def test_verify_loads_immutable_memory_copy():
    tid = "mem-copy"
    h = _prep_snapshot(tid)
    ok, reason, loaded, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok and reason == "ok"
    assert loaded["a.py"] == "new\n"
    # 磁盘再改不影响已返回的内存副本
    path = ws._abs(ws._task_root(tid), "approved_snapshot", "a.py")
    path.write_bytes(b"later\n")
    assert loaded["a.py"] == "new\n"


# ----- P1: auth on sensitive reads -----


def test_sensitive_reads_require_auth(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", "op-secret")
    monkeypatch.setenv("ALLOW_INSECURE_NO_AUTH", "false")
    get_settings.cache_clear()
    from app.main import app

    client = TestClient(app)
    paths = [
        "/api/approval/pending",
        "/api/approval/history",
        "/api/approval/tid-missing",
        "/api/chat/tasks",
        "/api/chat/tasks/t1",
        "/api/repos/o/r/tree",
        "/api/repos/o/r/file?path=a.py",
        "/api/github/permissions",
        "/api/eval/cases",
    ]
    for p in paths:
        r = client.get(p)
        assert r.status_code == 401, p
        body = r.json()
        detail = str(body.get("detail") or "")
        # 失败响应不得夹带敏感 payload
        assert "diff" not in body or body.get("diff") in (None, "")
        assert "items" not in body
        assert "ghp_" not in detail
        assert "token" not in detail.lower() or "Token" in detail or "认证" in detail

    # 错误 Token
    r_bad = client.get(
        "/api/approval/pending", headers={"X-Operator-Token": "wrong"}
    )
    assert r_bad.status_code == 401

    # 合法 Token
    r_ok = client.get(
        "/api/approval/pending", headers={"X-Operator-Token": "op-secret"}
    )
    assert r_ok.status_code == 200
    assert isinstance(r_ok.json(), list)

    # 健康检查公开
    r_h = client.get("/health")
    assert r_h.status_code == 200
    get_settings.cache_clear()


def test_auth_fail_closed_when_token_unset(monkeypatch):
    monkeypatch.setenv("OPERATOR_API_TOKEN", "")
    monkeypatch.setenv("ALLOW_INSECURE_NO_AUTH", "false")
    get_settings.cache_clear()
    from app.main import app

    client = TestClient(app)
    r = client.get("/api/chat/tasks")
    assert r.status_code == 401
    get_settings.cache_clear()


# ----- P1: test gate untrusted -----


def test_forged_passed_blocked_by_test_gate():
    ok, reason = evaluate_test_gate(
        {"passed": True, "not_run": False, "failed": False, "status": "succeeded"}
    )
    assert ok is False
    assert "untrusted_passed" in reason or "no_secure_executor" in reason


def test_forged_passed_publish_zero_writes(monkeypatch):
    calls = _gh_write_counters(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    tid = "forge-pass"
    h = _prep_snapshot(tid)
    env = asyncio.run(
        run_publish(
            task_id=tid,
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash=h,
            test_report={
                "passed": True,
                "not_run": False,
                "failed": False,
                "content_hash": h,
            },
        )
    )
    assert env.status == "failed"
    assert "test_gate" in (env.error or "")
    assert calls["create_branch"] == 0
    assert calls["update_file"] == 0
    assert calls["create_pr"] == 0


def test_reused_old_report_hash_mismatch(monkeypatch):
    calls = _gh_write_counters(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    monkeypatch.setattr(
        "app.agents.publish_agent.evaluate_test_gate",
        lambda *_a, **_k: (True, "ok"),
    )
    tid = "reuse-old"
    h = _prep_snapshot(tid)
    env = asyncio.run(
        run_publish(
            task_id=tid,
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash=h,
            test_report={
                "passed": True,
                "content_hash": "old-other-task-hash",
            },
        )
    )
    assert env.status == "failed"
    assert "test_report_content_mismatch" in (env.error or "")
    assert calls["create_branch"] == 0


def test_missing_empty_malformed_reports_blocked():
    for report in (None, {}, {"passed": True}, {"status": "weird"}):
        ok, reason = evaluate_test_gate(report)
        assert ok is False
        assert reason


def test_checkpoint_forged_passed_still_blocked(monkeypatch):
    """模拟从旧 checkpoint 恢复出的伪造 passed 报告。"""
    calls = _gh_write_counters(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    tid = "ckpt-forge"
    h = _prep_snapshot(tid)
    forged = {
        "passed": True,
        "not_run": False,
        "failed": False,
        "status": "succeeded",
        "content_hash": h,
        "trusted": True,  # 自填字段不得放行
        "from_checkpoint": True,
    }
    env = asyncio.run(
        run_publish(
            task_id=tid,
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash=h,
            test_report=forged,
        )
    )
    assert env.status == "failed"
    assert calls["create_branch"] == 0


# ----- P2: diff freeze version -----


def test_display_diff_matches_frozen_hash():
    tid = "diff-ver"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "a.py", "a\n")
    ws.write_work_file(tid, "a.py", "b\n")
    h = ws.freeze_approved_snapshot(tid)["content_hash"]
    snap = ws.snapshot_from_approved(tid, repo="o/r", ref="main")
    assert snap["content_hash"] == h
    assert "b" in snap["combined_diff"]
    # 改 work 不影响已展示冻结 Diff
    ws.write_work_file(tid, "a.py", "c\n")
    snap2 = ws.snapshot_from_approved(tid, repo="o/r", ref="main")
    assert snap2["content_hash"] == h
    assert "b" in snap2["combined_diff"]
    assert "c" not in snap2["combined_diff"]


def test_approve_after_snapshot_tamper_detected():
    tid = "approve-tamp"
    h = _prep_snapshot(tid)
    path = ws._abs(ws._task_root(tid), "approved_snapshot", "a.py")
    path.write_bytes(b"x\n")
    ok, reason, _, _ = ws.verify_and_load_approved_snapshot(tid, h)
    assert ok is False


def test_old_approval_hash_rejects_new_manifest():
    tid = "old-appr"
    h1 = _prep_snapshot(tid)
    ws.write_work_file(tid, "a.py", "newer\n")
    h2 = ws.freeze_approved_snapshot(tid)["content_hash"]
    assert h1 != h2
    ok, _, _, _ = ws.verify_and_load_approved_snapshot(tid, h1)
    assert ok is False


# ----- P2: legacy write tools + MCP -----


def test_tools_approved_rejects_write_tools():
    from app.agent.nodes import tools_approved_node

    state = {
        "trace_id": "legacy-1",
        "repo": "o/r",
        "approval_payload": {
            "pending_tool_calls": [
                {"name": "create_pr", "args": {"title": "x"}, "id": "1"},
                {"name": "update_file", "args": {"path": "a.py"}, "id": "2"},
                {"name": "create_branch", "args": {}, "id": "3"},
            ]
        },
    }

    async def _noop(*_a, **_k):
        return None

    import app.agent.nodes as nodes_mod
    from unittest.mock import patch

    with patch.object(nodes_mod.redis_service, "track_trajectory", _noop), patch.object(
        nodes_mod.redis_service, "resolve_approval", _noop
    ):
        out = asyncio.run(tools_approved_node(state))
    assert out.get("error") == "legacy_write_path_disabled"
    for item in out.get("last_tool_results") or []:
        assert item.get("blocked") is True


def test_mcp_lists_readonly_only():
    from app.mcp_server.protocol import call_tool, list_tool_names, tools_list_payload

    names = set(list_tool_names())
    for banned in ("create_pr", "update_file", "create_branch", "merge_pr", "push"):
        assert banned not in names
    assert "search_codebase" in names or "get_file_content" in names
    listed = {t["name"] for t in tools_list_payload()}
    assert listed == names

    async def _run():
        content = await call_tool("create_pr", {"repo": "o/r", "title": "x"})
        text = content[0]["text"]
        data = json.loads(text)
        assert data.get("ok") is False
        assert "只读" in data.get("error", "") or data.get("blocked_by") == "mcp_readonly"

    asyncio.run(_run())


# ----- P2: webhook -----


def test_webhook_secret_missing_rejects(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setenv("ALLOW_INSECURE_WEBHOOK", "false")
    get_settings.cache_clear()
    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/webhook/github",
        content=b'{"action":"opened"}',
        headers={"X-GitHub-Event": "issues"},
    )
    assert r.status_code == 401
    get_settings.cache_clear()


def test_webhook_bad_signature_rejects(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("ALLOW_INSECURE_WEBHOOK", "false")
    get_settings.cache_clear()
    from app.main import app

    client = TestClient(app)
    for sig in ("", "sha1=abc", "sha256=deadbeef"):
        r = client.post(
            "/api/webhook/github",
            content=b'{"action":"opened"}',
            headers={
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": sig,
            },
        )
        assert r.status_code == 401
    get_settings.cache_clear()


def test_webhook_valid_signature_acks_without_enqueue(monkeypatch):
    secret = "whsec-valid"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("ALLOW_INSECURE_WEBHOOK", "false")
    get_settings.cache_clear()
    from app.main import app

    body = b'{"action":"opened"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    client = TestClient(app)
    r = client.post(
        "/api/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": f"sha256={digest}",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert "no enqueue" in (data.get("detail") or "")
    get_settings.cache_clear()
