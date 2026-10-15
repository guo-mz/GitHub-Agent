"""审批 resume 门禁：缺失 / 已拒绝 / 已过期 / 状态不匹配 → 默认拒绝。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import approval_store, task_run_service as trs
from app.services.approval_gate import validate_approval_resume


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(approval_store, "STORE_PATH", tmp_path / "approvals.json")
    monkeypatch.setattr(trs, "STORE_PATH", tmp_path / "task_runs.json")
    trs._CANCELLED.clear()
    approval_store.clear_all_for_tests()
    yield
    trs._CANCELLED.clear()


def test_missing_pending_denied():
    ok, reason, _ = validate_approval_resume("tid-missing", decision="approve")
    assert ok is False
    assert "no pending" in reason


def test_resolved_reject_blocks_reapprove():
    approval_store.mark_resolved("tid-r", decision="reject", approval_kind="plan_confirm")
    ok, reason, _ = validate_approval_resume(
        "tid-r", decision="approve", approval_kind="plan_confirm"
    )
    assert ok is False
    assert "already resolved" in reason


def test_disk_resolved_survives_without_redis_memory():
    """模拟进程重启：仅磁盘有 resolved，热缓存为空。"""
    approval_store.save_pending(
        "tid-disk",
        {"thread_id": "tid-disk", "approval_kind": "plan_confirm", "status": "pending"},
    )
    approval_store.mark_resolved("tid-disk", decision="reject", approval_kind="plan_confirm")
    assert approval_store.get_pending("tid-disk") is None
    assert approval_store.get_resolved("tid-disk")
    ok, reason, _ = validate_approval_resume("tid-disk", decision="approve")
    assert ok is False
    assert "already resolved" in reason


def test_expired_pending_denied():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pending = {
        "thread_id": "tid-exp",
        "approval_kind": "plan_confirm",
        "status": "pending",
        "updated_at": old,
    }
    approval_store.save_pending("tid-exp", pending)
    # save_pending 会刷新 updated_at，直接传入旧 pending
    ok, reason, _ = validate_approval_resume(
        "tid-exp",
        decision="approve",
        pending={**pending, "updated_at": old},
        max_pending_age_sec=3600,
    )
    assert ok is False
    assert "expired" in reason


def test_status_mismatch_denied():
    tid = "tid-mismatch"
    trs.create_run(thread_id=tid, repo="o/r", message="x")
    trs.apply_terminal(tid, status="cancelled", error="plan_rejected")
    pending = {
        "thread_id": tid,
        "approval_kind": "plan_confirm",
        "status": "pending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ok, reason, _ = validate_approval_resume(tid, decision="approve", pending=pending)
    assert ok is False
    assert "incompatible" in reason or "cancelled" in reason


def test_awaiting_approval_allows_resume():
    tid = "tid-ok"
    trs.create_run(thread_id=tid, repo="o/r", message="x")
    trs.apply_approval_needed(tid)
    pending = {
        "thread_id": tid,
        "task_id": tid,
        "approval_kind": "plan_confirm",
        "status": "pending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    approval_store.save_pending(tid, pending)
    ok, reason, p = validate_approval_resume(
        tid, decision="approve", approval_kind="plan_confirm", task_id=tid
    )
    assert ok is True
    assert reason == "ok"
    assert p is not None


def test_kind_mismatch_denied():
    tid = "tid-kind"
    trs.create_run(thread_id=tid, repo="o/r", message="x")
    trs.apply_approval_needed(tid)
    pending = {
        "thread_id": tid,
        "approval_kind": "plan_confirm",
        "status": "pending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ok, reason, _ = validate_approval_resume(
        tid, decision="approve", approval_kind="publish", pending=pending
    )
    assert ok is False
    assert "approval_kind mismatch" in reason
