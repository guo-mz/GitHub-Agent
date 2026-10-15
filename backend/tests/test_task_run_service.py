"""任务状态持久化与工具证据终态规则。"""

from __future__ import annotations

import uuid

import pytest

from app.services import task_run_service as trs


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    path = tmp_path / "task_runs.json"
    monkeypatch.setattr(trs, "STORE_PATH", path)
    trs._CANCELLED.clear()
    yield
    trs._CANCELLED.clear()


def test_create_and_stage_progress():
    tid = uuid.uuid4().hex
    run = trs.create_run(thread_id=tid, repo="o/r", message="fix bug")
    assert run["status"] == "queued"
    trs.apply_node_event(tid, node="classify_intent", status="enter", label="意图")
    mid = trs.get_run_by_thread(tid)
    assert mid["status"] == "analyzing"
    assert any(s["id"] == "understand" for s in mid["stages"])
    view = trs.public_view(mid)
    assert view["elapsed_ms"] is not None


def test_publish_without_evidence_cannot_succeed():
    tid = uuid.uuid4().hex
    trs.create_run(thread_id=tid, repo="o/r", message="pr")
    trs.apply_tool_event(tid, tool="create_pull_request", phase="start")
    trs.apply_tool_event(
        tid,
        tool="create_pull_request",
        phase="end",
        output='{"ok": false, "error": "denied"}',
    )
    final = trs.apply_terminal(tid, status="succeeded")
    assert final["status"] == "failed"


def test_publish_ok_allows_succeed():
    tid = uuid.uuid4().hex
    trs.create_run(thread_id=tid, repo="o/r", message="pr")
    trs.apply_tool_event(tid, tool="create_pull_request", phase="start")
    trs.apply_tool_event(
        tid,
        tool="create_pull_request",
        phase="end",
        output='{"ok": true, "html_url": "https://github.com/o/r/pull/1"}',
    )
    final = trs.apply_terminal(tid, status="succeeded")
    assert final["status"] == "succeeded"
    assert final["external"]["pr_url"]


def test_cancel_flag():
    tid = uuid.uuid4().hex
    trs.create_run(thread_id=tid, repo="o/r", message="x")
    trs.apply_node_event(tid, node="classify_intent", status="enter")
    trs.request_cancel(tid)
    assert trs.is_cancel_requested(tid)


def test_plan_confirmed_soft_terminal_and_diff_approved():
    """计划确认 / Diff 确认 ≠ 远程写成功。"""
    tid = uuid.uuid4().hex
    trs.create_run(thread_id=tid, repo="o/r", message="write something")
    trs.apply_node_event(tid, node="await_plan_confirm", status="enter")
    final = trs.apply_awaiting_coding(tid, final_note="计划已确认，等待执行")
    assert final["status"] == "awaiting_coding"
    assert final["current_step"] == "计划已确认，等待执行"
    ok, reason = trs.can_retry(final)
    assert ok is False

    tid2 = uuid.uuid4().hex
    trs.create_run(thread_id=tid2, repo="o/r", message="x")
    trs.apply_node_event(tid2, node="coding_agent", status="enter")
    trs.apply_node_event(tid2, node="coding_agent", status="done")
    trs.apply_node_event(tid2, node="await_diff_approve", status="enter")
    done = trs.apply_diff_approved(tid2, final_note="Diff 已确认")
    assert done["status"] == "diff_approved"
    assert "Diff" in (done.get("current_step") or "")
    ok2, _ = trs.can_retry(done)
    assert ok2 is False


def test_ui_snapshot_and_publish_evidence():
    tid = uuid.uuid4().hex
    trs.create_run(thread_id=tid, repo="o/r", message="x")
    snap = trs.patch_ui_snapshot(
        tid,
        phase="awaiting_publish",
        agent_envelopes=[{"agent_name": "testing", "status": "not_run"}],
        test_report={"not_run": True, "passed": False},
        diff_approved=True,
    )
    assert snap["phase"] == "awaiting_publish"
    view = trs.public_view(snap)
    assert view["agent_envelopes"][0]["agent_name"] == "testing"
    assert view["test_report"]["not_run"] is True
    assert view["diff_approved"] is True

    pub = trs.record_publish_success(
        tid,
        pr_url="https://github.com/o/r/pull/3",
        pr_number=3,
        branch="agent/phase3-x",
        commit_sha="abc",
    )
    assert pub["tool_evidence"]["publish_ok"] is True
    assert pub["external"]["pr_url"].endswith("/pull/3")
    final = trs.apply_terminal(tid, status="succeeded", final_note="published")
    assert final["status"] == "succeeded"


def test_retry_blocked_after_publish_ok():
    tid = uuid.uuid4().hex
    trs.create_run(thread_id=tid, repo="o/r", message="x")
    trs.apply_terminal(tid, status="failed", error="boom")
    ok, _ = trs.can_retry(trs.get_run_by_thread(tid))
    assert ok is True
    with trs._LOCK:
        items = trs._load()
        for i, it in enumerate(items):
            if it["thread_id"] == tid:
                items[i] = {
                    **it,
                    "status": "failed",
                    "tool_evidence": {**(it.get("tool_evidence") or {}), "publish_ok": True},
                    "external": {"pr_url": "https://github.com/o/r/pull/2"},
                }
                trs._save(items)
    ok2, reason = trs.can_retry(trs.get_run_by_thread(tid))
    assert ok2 is False
    assert "PR" in reason or "发布" in reason
