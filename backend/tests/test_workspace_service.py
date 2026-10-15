"""隔离工作区 + 真实 Diff（非模型口述）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import workspace_service as ws


@pytest.fixture(autouse=True)
def _tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "ROOT", tmp_path / "workspaces")
    yield


def test_write_work_only_and_real_diff():
    tid = "ws-1"
    ws.ensure_workspace(tid, repo="o/r", ref="main")
    ws.write_base_file(tid, "README.md", "hello\n")
    assert ws.read_work_file(tid, "README.md") == "hello\n"
    ws.write_work_file(tid, "README.md", "hello\nworld\n")
    d = ws.compute_file_diff(tid, "README.md")
    assert d is not None
    assert d.status == "modified"
    assert "world" in d.unified_diff
    assert d.unified_diff.startswith("---") or "--- a/README.md" in d.unified_diff
    snap = ws.snapshot_to_dict(tid, repo="o/r", ref="main")
    assert snap["stats"]["files"] == 1
    assert "world" in snap["combined_diff"]


def test_path_escape_blocked():
    tid = "ws-2"
    ws.ensure_workspace(tid, repo="o/r")
    with pytest.raises(ValueError):
        ws.write_work_file(tid, "../evil.txt", "x")


def test_discard_workspace_removes_work_files():
    tid = "ws-3"
    ws.ensure_workspace(tid, repo="o/r")
    ws.write_base_file(tid, "a.txt", "1")
    ws.write_work_file(tid, "a.txt", "2")
    ws.discard_workspace(tid)
    # base 仍在
    assert ws.read_base_file(tid, "a.txt") == "1"
    # work 清空
    assert ws.list_work_rel_paths(tid) == []


def test_freeze_nested_path_skips_parent_dirs():
    """docs/hello.md 冻结时不得因目录 docs 报 not_regular_file。"""
    tid = "ws-nested"
    ws.ensure_workspace(tid, repo="o/r", ref="main")
    ws.write_base_file(tid, "docs/hello.md", "old\n")
    ws.write_work_file(tid, "docs/hello.md", "new content\n")
    manifest = ws.freeze_approved_snapshot(tid)
    assert manifest.get("content_hash")
    paths, reason = ws.list_approved_snapshot_rel_paths(tid)
    assert reason == "ok"
    assert paths == ["docs/hello.md"]
    snap = ws.snapshot_from_approved(tid, repo="o/r", ref="main")
    assert snap.get("combined_diff")
    assert any(d.get("path") == "docs/hello.md" for d in (snap.get("diffs") or []))
