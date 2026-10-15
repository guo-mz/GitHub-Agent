"""Coding Agent：计划未确认禁止写；工作区产出真实 Diff。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents import coding_agent as ca
from app.services import workspace_service as ws


@pytest.fixture(autouse=True)
def _tmp_ws(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "ROOT", tmp_path / "workspaces")
    yield


def _run(coro):
    return asyncio.run(coro)


def test_coding_blocked_without_plan_confirm():
    env = _run(
        ca.run_coding(
            task_id="c1",
            repo="o/r",
            user_message="edit",
            plan_document={"files_to_change": ["a.txt"]},
            plan_confirmed=False,
        )
    )
    assert env.status == "failed"
    assert env.error == "plan_not_confirmed"


def test_coding_writes_workspace_and_real_diff(monkeypatch):
    async def fake_fetch(task_id, repo, paths, ref="main"):
        for p in paths:
            ws.ensure_workspace(task_id, repo=repo, ref=ref)
            ws.write_base_file(task_id, p, "line1\n")
        return list(paths)

    async def fake_llm(messages, temperature=0.1, label="llm", timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "files": [{"path": "README.md", "content": "line1\nline2\n"}],
                    "notes": ["append"],
                }
            )
        )

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    monkeypatch.setattr(ca, "ainvoke_llm", fake_llm)

    env = _run(
        ca.run_coding(
            task_id="c2",
            repo="o/r",
            user_message="append line",
            plan_document={"files_to_change": ["README.md"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "succeeded"
    assert env.artifacts.get("combined_diff")
    assert "line2" in (env.artifacts.get("combined_diff") or "")
    assert ws.read_work_file("c2", "README.md") == "line1\nline2\n"
    assert ws.read_base_file("c2", "README.md") == "line1\n"


def test_coding_rejects_unconditional_single_file_remap(monkeypatch):
    """单文件计划不得把模型任意文件名无条件映射到计划路径。"""

    async def fake_fetch(task_id, repo, paths, ref="main"):
        for p in paths:
            ws.ensure_workspace(task_id, repo=repo, ref=ref)
            ws.write_base_file(task_id, p, "hello from agent\n")
        return list(paths)

    async def fake_llm(messages, temperature=0.1, label="llm", timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "files": [
                        {
                            "path": "fastapi-tutorial.md",
                            "content": "# FastAPI\n\n教程正文\n",
                        }
                    ],
                    "notes": ["renamed wrongly"],
                }
            )
        )

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    monkeypatch.setattr(ca, "ainvoke_llm", fake_llm)

    env = _run(
        ca.run_coding(
            task_id="c3",
            repo="o/r",
            user_message="写 fastapi 教程",
            plan_document={"files_to_change": ["docs/hello.md"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "failed"
    assert "coding_paths_mismatch" in (env.error or "")
    # 不得写入计划外路径，也不得静默落到 docs/hello.md
    assert ws.read_work_file("c3", "docs/hello.md") == "hello from agent\n"
    assert ws.read_work_file("c3", "fastapi-tutorial.md") is None


def test_coding_basename_match_still_allowed(monkeypatch):
    """唯一 basename 匹配仍可落到计划路径（非无条件任意映射）。"""

    async def fake_fetch(task_id, repo, paths, ref="main"):
        for p in paths:
            ws.ensure_workspace(task_id, repo=repo, ref=ref)
            ws.write_base_file(task_id, p, "old\n")
        return list(paths)

    async def fake_llm(messages, temperature=0.1, label="llm", timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "files": [{"path": "hello.md", "content": "new\n"}],
                    "notes": ["basename"],
                }
            )
        )

    monkeypatch.setattr(ca, "_fetch_into_workspace", fake_fetch)
    monkeypatch.setattr(ca, "ainvoke_llm", fake_llm)

    env = _run(
        ca.run_coding(
            task_id="c3b",
            repo="o/r",
            user_message="edit",
            plan_document={"files_to_change": ["docs/hello.md"], "title": "t"},
            plan_confirmed=True,
        )
    )
    assert env.status == "succeeded"
    assert ws.read_work_file("c3b", "docs/hello.md") == "new\n"
