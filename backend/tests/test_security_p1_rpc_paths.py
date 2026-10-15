"""P1 修复验证：内部 RPC 凭据 + Coding/Publish 敏感路径护栏。

隔离环境：不连真实 GitHub、不提交真实代码。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents import coding_agent as ca
from app.agents import publish_agent
from app.api import internal_github
from app.config import get_settings
from app.core import internal_rpc_auth
from app.services import approval_store, workspace_service as ws
from app.services.guardrails import (
    PathSecurityError,
    check_path_whitelist,
    validate_write_path,
)


STRONG_TOKEN = "test-internal-rpc-token-ok"
WEAK_DEFAULT = "dev-internal-rpc"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_store, "STORE_PATH", tmp_path / "approvals.json")
    monkeypatch.setattr(ws, "ROOT", tmp_path / "workspaces")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _rpc_client(monkeypatch, token_env: str | None) -> TestClient:
    if token_env is None:
        monkeypatch.delenv("INTERNAL_RPC_TOKEN", raising=False)
        monkeypatch.setenv("INTERNAL_RPC_TOKEN", "")
    else:
        monkeypatch.setenv("INTERNAL_RPC_TOKEN", token_env)
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(internal_github.router)
    return TestClient(app)


def _run(coro):
    return asyncio.run(coro)


# ---------- P1-1: internal RPC auth ----------


def test_rpc_rejects_unset_token(monkeypatch):
    client = _rpc_client(monkeypatch, "")
    r = client.post(
        "/internal/v1/github/call",
        json={"method": "get_repo_tree", "args": {"owner": "o", "repo": "r"}},
        headers={"X-Internal-Token": "anything-long-enough"},
    )
    assert r.status_code == 401
    detail = str(r.json().get("detail") or "")
    assert "INTERNAL_RPC_TOKEN" in detail
    assert WEAK_DEFAULT not in detail
    assert STRONG_TOKEN not in detail


def test_rpc_rejects_empty_token_header(monkeypatch):
    client = _rpc_client(monkeypatch, STRONG_TOKEN)
    r = client.post(
        "/internal/v1/github/call",
        json={"method": "get_repo_tree", "args": {"owner": "o", "repo": "r"}},
    )
    assert r.status_code == 401


def test_rpc_rejects_wrong_token(monkeypatch):
    client = _rpc_client(monkeypatch, STRONG_TOKEN)
    r = client.post(
        "/internal/v1/github/call",
        json={"method": "get_repo_tree", "args": {"owner": "o", "repo": "r"}},
        headers={"X-Internal-Token": "wrong-token-value!!"},
    )
    assert r.status_code == 401
    assert "invalid" in str(r.json().get("detail") or "").lower()


def test_rpc_rejects_weak_default_even_if_configured(monkeypatch):
    """生产/任意配置下，dev-internal-rpc 不得作为有效凭据。"""
    client = _rpc_client(monkeypatch, WEAK_DEFAULT)
    assert internal_rpc_auth.resolve_internal_rpc_token() == ""
    r = client.post(
        "/internal/v1/github/call",
        json={"method": "get_repo_tree", "args": {"owner": "o", "repo": "r"}},
        headers={"X-Internal-Token": WEAK_DEFAULT},
    )
    assert r.status_code == 401


def test_rpc_rejects_short_token(monkeypatch):
    client = _rpc_client(monkeypatch, "short-token")
    r = client.post(
        "/internal/v1/github/call",
        json={"method": "get_repo_tree", "args": {"owner": "o", "repo": "r"}},
        headers={"X-Internal-Token": "short-token"},
    )
    assert r.status_code == 401


def test_rpc_accepts_valid_token_and_readonly_call(monkeypatch):
    client = _rpc_client(monkeypatch, STRONG_TOKEN)

    async def _tree(*_a, **_k):
        return {"tree": [{"path": "a.py", "type": "blob"}]}

    monkeypatch.setattr(internal_github.github_client, "get_repo_tree", _tree)
    r = client.post(
        "/internal/v1/github/call",
        json={
            "method": "get_repo_tree",
            "args": {"owner": "o", "repo": "r", "branch": "main"},
        },
        headers={"X-Internal-Token": STRONG_TOKEN},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data["result"]["tree"][0]["path"] == "a.py"


def test_rpc_write_method_denied_even_with_valid_token(monkeypatch):
    client = _rpc_client(monkeypatch, STRONG_TOKEN)
    called = {"n": 0}

    async def _upd(*_a, **_k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(internal_github.github_client, "update_file", _upd)
    r = client.post(
        "/internal/v1/github/call",
        json={
            "method": "update_file",
            "args": {
                "owner": "o",
                "repo": "r",
                "path": "a.py",
                "content": "x",
                "message": "m",
                "branch": "b",
                "sha": "",
            },
        },
        headers={"X-Internal-Token": STRONG_TOKEN},
    )
    assert r.status_code == 403
    assert called["n"] == 0


def test_rpc_unknown_method_rejected(monkeypatch):
    client = _rpc_client(monkeypatch, STRONG_TOKEN)
    r = client.post(
        "/internal/v1/github/call",
        json={"method": "eval_code", "args": {}},
        headers={"X-Internal-Token": STRONG_TOKEN},
    )
    assert r.status_code == 400


def test_rpc_client_fails_closed_without_token(monkeypatch):
    monkeypatch.setenv("INTERNAL_RPC_TOKEN", "")
    get_settings.cache_clear()
    from app.services.github_client import GitHubAPIError
    from app.services.github_rpc import GitHubRPC

    rpc = GitHubRPC()

    async def _boom(*_a, **_k):
        raise AssertionError("HTTP must not be called")

    monkeypatch.setattr(rpc, "_http", _boom)
    with pytest.raises(GitHubAPIError) as ei:
        _run(rpc.call("get_repo_tree", owner="o", repo="r"))
    assert ei.value.status == 401


def test_tokens_match_uses_compare_digest():
    assert internal_rpc_auth.tokens_match(STRONG_TOKEN, STRONG_TOKEN) is True
    assert internal_rpc_auth.tokens_match("a", "b") is False
    assert internal_rpc_auth.tokens_match("", STRONG_TOKEN) is False
    assert internal_rpc_auth.tokens_match(None, STRONG_TOKEN) is False
    assert internal_rpc_auth.tokens_match("short", STRONG_TOKEN) is False


def test_config_default_internal_rpc_token_empty():
    """默认不得再是可预测的 dev-internal-rpc。"""
    from app.config import Settings

    field_default = Settings.model_fields["internal_rpc_token"].default
    assert field_default == ""
    assert field_default != WEAK_DEFAULT


# ---------- P1-2: path guardrails ----------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env",
        "./.env",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "secrets/key.pem",
        "../evil.txt",
        "/etc/passwd",
        r"C:\Windows\system32\config",
        "foo/../../.env",
    ],
)
def test_validate_write_path_blocks_sensitive_and_traversal(path):
    with pytest.raises(PathSecurityError):
        validate_write_path(path)
    assert check_path_whitelist([path]) is not None


def test_validate_write_path_allows_normal_files():
    assert validate_write_path("README.md") == "README.md"
    assert validate_write_path("src/app.py") == "src/app.py"
    assert validate_write_path("./docs/hello.md") == "docs/hello.md"
    assert check_path_whitelist(["README.md", "src/app.py"]) is None


def test_coding_rejects_sensitive_planned_path(monkeypatch):
    async def fake_fetch(*_a, **_k):
        raise AssertionError("must not fetch sensitive path")

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    env = _run(
        ca.run_coding(
            task_id="c-env",
            repo="o/r",
            user_message="leak",
            plan_document={"files_to_change": [".env"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "failed"
    assert "sensitive_path_blocked" in (env.error or "")


def test_coding_rejects_model_writing_dotenv(monkeypatch):
    async def fake_fetch(task_id, repo, paths, ref="main"):
        for p in paths:
            ws.ensure_workspace(task_id, repo=repo, ref=ref)
            ws.write_base_file(task_id, p, "ok\n")
        return list(paths)

    async def fake_llm(messages, temperature=0.1, label="llm", timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "files": [
                        {"path": "README.md", "content": "x\n"},
                        {"path": ".env", "content": "SECRET=1\n"},
                    ]
                }
            )
        )

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    monkeypatch.setattr(ca, "ainvoke_llm", fake_llm)
    env = _run(
        ca.run_coding(
            task_id="c-dotenv",
            repo="o/r",
            user_message="edit",
            plan_document={"files_to_change": ["README.md"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "failed"
    assert "sensitive_path_blocked" in (env.error or "")
    assert ws.read_work_file("c-dotenv", ".env") is None


def test_coding_rejects_path_traversal(monkeypatch):
    async def fake_fetch(task_id, repo, paths, ref="main"):
        for p in paths:
            ws.ensure_workspace(task_id, repo=repo, ref=ref)
            ws.write_base_file(task_id, p, "ok\n")
        return list(paths)

    async def fake_llm(messages, temperature=0.1, label="llm", timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {"files": [{"path": "../outside.txt", "content": "x\n"}]}
            )
        )

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    monkeypatch.setattr(ca, "ainvoke_llm", fake_llm)
    env = _run(
        ca.run_coding(
            task_id="c-trav",
            repo="o/r",
            user_message="edit",
            plan_document={"files_to_change": ["a.py"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "failed"
    assert "sensitive_path_blocked" in (env.error or "")


def test_coding_allows_normal_file(monkeypatch):
    async def fake_fetch(task_id, repo, paths, ref="main"):
        for p in paths:
            ws.ensure_workspace(task_id, repo=repo, ref=ref)
            ws.write_base_file(task_id, p, "old\n")
        return list(paths)

    async def fake_llm(messages, temperature=0.1, label="llm", timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {"files": [{"path": "src/app.py", "content": "new\n"}]}
            )
        )

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    monkeypatch.setattr(ca, "ainvoke_llm", fake_llm)
    env = _run(
        ca.run_coding(
            task_id="c-ok",
            repo="o/r",
            user_message="edit",
            plan_document={"files_to_change": ["src/app.py"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "succeeded"
    assert ws.read_work_file("c-ok", "src/app.py") == "new\n"


def _gh_write_counters(monkeypatch, *, stub_create_pr: bool = False):
    calls = {"create_branch": 0, "update_file": 0, "create_pr": 0}

    async def _cb(*_a, **_k):
        calls["create_branch"] += 1
        return {"ref": "refs/heads/x"}

    async def _uf(*_a, **_k):
        calls["update_file"] += 1
        return {"commit": {"sha": "abc"}}

    from app.services.github_client import GitHubAPIError

    monkeypatch.setattr("app.agents.publish_agent.github_client.create_branch", _cb)
    monkeypatch.setattr("app.agents.publish_agent.github_client.update_file", _uf)
    monkeypatch.setattr(
        "app.agents.publish_agent.github_client.get_file_content",
        AsyncMock(side_effect=GitHubAPIError(404, "not found")),
    )
    if stub_create_pr:
        import sys
        import types

        async def _pr(*_a, **_k):
            calls["create_pr"] += 1
            return json.dumps(
                {
                    "ok": True,
                    "html_url": "https://github.com/o/r/pull/1",
                    "number": 1,
                }
            )

        fake_tool = MagicMock()
        fake_tool.ainvoke = AsyncMock(side_effect=_pr)
        # 注入独立模块，避免 import app.tools 触发 __init__ 副作用
        mod = types.ModuleType("app.tools.github_tools")
        mod.create_pr = fake_tool
        sys.modules["app.tools.github_tools"] = mod
    return calls


def test_publish_rejects_sensitive_path_despite_approvals(monkeypatch):
    """人工审批已通过，快照含 .env 时仍拒绝且零远程写。"""
    calls = _gh_write_counters(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    monkeypatch.setattr(
        "app.agents.publish_agent.evaluate_test_gate",
        lambda *_a, **_k: (True, "ok"),
    )

    tid = "pub-env"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, ".env", "OLD=1\n")
    ws.write_work_file(tid, ".env", "SECRET=leak\n")
    h = ws.freeze_approved_snapshot(tid)["content_hash"]

    env = _run(
        publish_agent.run_publish(
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
    assert "sensitive_path_blocked" in (env.error or "")
    assert calls["create_branch"] == 0
    assert calls["update_file"] == 0
    assert calls["create_pr"] == 0


def test_publish_allows_normal_file(monkeypatch):
    calls = _gh_write_counters(monkeypatch, stub_create_pr=True)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    monkeypatch.setattr(
        "app.agents.publish_agent.evaluate_test_gate",
        lambda *_a, **_k: (True, "ok"),
    )

    async def _acquire(*_a, **_k):
        return SimpleNamespace(acquired=True, cached_value=None)

    async def _set(*_a, **_k):
        return None

    async def _rel(*_a, **_k):
        return None

    monkeypatch.setattr(
        "app.agents.publish_agent.redis_service.acquire_idempotency", _acquire
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.redis_service.set_idempotency_result", _set
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.redis_service.release_idempotency", _rel
    )

    tid = "pub-ok"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "src/app.py", "old\n")
    ws.write_work_file(tid, "src/app.py", "new\n")
    h = ws.freeze_approved_snapshot(tid)["content_hash"]

    env = _run(
        publish_agent.run_publish(
            task_id=tid,
            repo="o/r",
            plan_document={"title": "ok"},
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash=h,
            test_report={"passed": True, "content_hash": h},
        )
    )
    assert env.status == "succeeded"
    assert calls["update_file"] == 1
    assert calls["create_pr"] == 1


def test_workspace_symlink_escape_blocked(tmp_path):
    tid = "sym-esc"
    ws.ensure_workspace(tid, repo="o/r")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    work = ws._task_root(tid) / "work"
    link = work / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not permitted on this host")
    # 经 normalize + resolve 的写入不得逃出 work
    with pytest.raises(ValueError):
        ws.write_work_file(tid, "../outside.txt", "x")
    # 敏感护栏独立拒绝穿越
    with pytest.raises(PathSecurityError):
        validate_write_path("../outside.txt")


def test_error_messages_do_not_leak_tokens():
    err = PathSecurityError("sensitive_path_forbidden", ".env")
    text = str(err)
    assert "SECRET" not in text
    assert "ghp_" not in text
    assert WEAK_DEFAULT not in text or ".env" in text
