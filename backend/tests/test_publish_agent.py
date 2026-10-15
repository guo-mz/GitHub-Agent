"""Publish Agent：三门 / 测试门禁 / 哈希 / 幂等。"""

import asyncio
from types import SimpleNamespace

from app.agents import publish_agent
from app.config import get_settings


def _passed_report(content_hash: str = "abc") -> dict:
    return {
        "status": "succeeded",
        "passed": True,
        "failed": False,
        "not_run": False,
        "unavailable": False,
        "content_hash": content_hash,
    }


def _force_secure_publish_settings(monkeypatch):
    """忽略本地 .env 演示开关，断言默认安全语义。"""
    base = get_settings()
    secure = SimpleNamespace(
        **{
            **base.model_dump(),
            "allow_untrusted_publish": False,
            "allow_publish_without_redis": False,
        }
    )
    monkeypatch.setattr("app.config.get_settings", lambda: secure)


def test_publish_blocked_without_triple_gate():
    env = asyncio.run(
        publish_agent.run_publish(
            task_id="tid-block",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=False,
        )
    )
    assert env.status == "failed"
    assert env.error == "publish_gate_blocked"


def test_publish_blocked_missing_diff_approve():
    env = asyncio.run(
        publish_agent.run_publish(
            task_id="tid-block2",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=False,
            publish_approved=True,
        )
    )
    assert env.status == "failed"
    assert env.error == "publish_gate_blocked"


def test_publish_blocked_when_test_not_run(monkeypatch):
    _force_secure_publish_settings(monkeypatch)
    env = asyncio.run(
        publish_agent.run_publish(
            task_id="tid-tr",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash="h1",
            test_report={
                "status": "not_run",
                "passed": False,
                "not_run": True,
                "unavailable": True,
            },
        )
    )
    assert env.status == "failed"
    assert "test_gate_blocked" in (env.error or "")


def test_evaluate_test_gate_matrix():
    """无安全执行器：任何 passed 字典均不得放行。"""
    for bad in (
        {"passed": True, "not_run": False},
        {"not_run": True, "passed": False},
        {"failed": True, "passed": False},
        {"unavailable": True},
        {"status": "timeout"},
        {"status": "error"},
        {"passed": False},
        {},
    ):
        ok, reason = publish_agent.evaluate_test_gate(bad)
        assert ok is False
        assert reason


def test_evaluate_test_gate_local_untrusted_bypass():
    """本地演示旁路：仅放行 not_run/unavailable，仍拒伪造 passed 与 failed。"""
    ok, reason = publish_agent.evaluate_test_gate(
        {"not_run": True, "unavailable": True, "passed": False},
        allow_untrusted=True,
    )
    assert ok is True
    assert "local_untrusted" in reason

    ok2, _ = publish_agent.evaluate_test_gate(
        {"passed": True, "not_run": False},
        allow_untrusted=True,
    )
    assert ok2 is False

    ok3, _ = publish_agent.evaluate_test_gate(
        {"failed": True, "status": "failed"},
        allow_untrusted=True,
    )
    assert ok3 is False


def test_publish_idempotent_cache_unreachable_without_executor(monkeypatch):
    """幂等缓存命中路径在测试门禁之后；无执行器时不得到达远程写。"""
    class _Lock:
        acquired = False
        cached_value = (
            '{"ok": true, "pr_url": "https://github.com/o/r/pull/9", '
            '"pr_number": 9, "branch": "agent/phase3-x"}'
        )

    async def _acquire(*_a, **_k):
        return _Lock()

    monkeypatch.setattr(
        "app.agents.publish_agent.redis_service.acquire_idempotency", _acquire
    )
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", False)
    env = asyncio.run(
        publish_agent.run_publish(
            task_id="tid-idem",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash="h-idem",
            test_report=_passed_report("h-idem"),
        )
    )
    assert env.status == "failed"
    assert "test_gate" in (env.error or "")


def test_publish_rejects_hash_mismatch(monkeypatch):
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
        publish_agent.run_publish(
            task_id="tid-hash",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash="old",
            test_report=_passed_report("old"),
        )
    )
    assert env.status == "failed"
    assert "snapshot_integrity" in (env.error or "") or "content_hash" in (
        env.error or ""
    )


def test_publish_rejects_without_redis(monkeypatch):
    _force_secure_publish_settings(monkeypatch)
    monkeypatch.setattr("app.agents.publish_agent.redis_service.fallback", True)
    monkeypatch.setattr(
        "app.agents.publish_agent.evaluate_test_gate",
        lambda *_a, **_k: (True, "ok"),
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.ws.verify_and_load_approved_snapshot",
        lambda *_a, **_k: (
            True,
            "ok",
            {"a.py": "x"},
            {
                "content_hash": "h",
                "files": [{"path": "a.py", "status": "modified"}],
            },
        ),
    )
    monkeypatch.setattr(
        "app.agents.publish_agent.ws.build_publish_manifest",
        lambda *_a, **_k: {"content_hash": "h"},
    )
    env = asyncio.run(
        publish_agent.run_publish(
            task_id="tid-noredis",
            repo="o/r",
            plan_confirmed=True,
            diff_approved=True,
            publish_approved=True,
            approved_content_hash="h",
            test_report=_passed_report("h"),
        )
    )
    assert env.status == "failed"
    assert "idempotency_unavailable" in (env.error or "")
