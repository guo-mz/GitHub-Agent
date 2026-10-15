"""任务执行状态持久化（本地 JSON，与 audit_log 同模式）。

后端为状态真实来源：status / stages / logs / 起止时间 / 错误信息。
LLM 自然语言不得单独将任务标为成功；仅工具结果与终态事件可推进终态。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

STORE_PATH = Path(__file__).resolve().parents[2] / ".data" / "task_runs.json"
_LOCK = threading.RLock()
_MAX = 300

# 进程内取消标记（图循环轮询）；持久化 status=cancelled 也作为真相源
_CANCELLED: Set[str] = set()

TaskStatus = str  # queued|analyzing|planning|editing|checking|awaiting_approval|awaiting_coding|publishing|succeeded|failed|cancelled

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
# 计划已确认、编码未执行：软终态（可停表，但不是「代码修改成功」）
SOFT_TERMINAL = frozenset({"awaiting_coding", "diff_approved"})

# 用户可见阶段（按适用性动态出现）
STAGE_DEFS = [
    {"id": "understand", "label": "需求理解"},
    {"id": "analyze_repo", "label": "仓库分析"},
    {"id": "plan", "label": "生成修改计划"},
    {"id": "plan_confirm", "label": "确认计划"},
    {"id": "await_coding", "label": "等待执行"},
    {"id": "edit", "label": "修改文件（工作区）"},
    {"id": "check", "label": "代码检查"},
    {"id": "await_review", "label": "等待审核 Diff"},
    {"id": "await_publish", "label": "等待发布审批"},
    {"id": "publish", "label": "提交 GitHub"},
]

# LangGraph 节点 → 任务状态 + 阶段
_NODE_TO_STATUS: Dict[str, str] = {
    "start": "queued",
    "classify_intent": "analyzing",
    "jev_decide": "analyzing",
    "repo_analysis": "analyzing",
    "planning_agent": "planning",
    "await_plan_confirm": "awaiting_approval",
    "coding_agent": "editing",
    "await_diff_approve": "awaiting_approval",
    "testing_agent": "checking",
    "await_publish_approve": "awaiting_approval",
    "publish_agent": "publishing",
    "coding_deferred": "editing",
    "planner": "planning",
    "compact_context": "planning",
    "agent": "analyzing",
    "tools": "editing",
    "guardrail_check": "checking",
    "request_approval": "awaiting_approval",
    "tools_approved": "publishing",
    "reject": "cancelled",
    "finalize": "analyzing",
}

_NODE_TO_STAGE: Dict[str, str] = {
    "classify_intent": "understand",
    "jev_decide": "understand",
    "repo_analysis": "analyze_repo",
    "planning_agent": "plan",
    "await_plan_confirm": "plan_confirm",
    "coding_agent": "edit",
    "await_diff_approve": "await_review",
    "testing_agent": "check",
    "await_publish_approve": "await_publish",
    "publish_agent": "publish",
    "coding_deferred": "edit",
    "planner": "plan",
    "compact_context": "plan",
    "agent": "understand",
    "tools": "edit",
    "guardrail_check": "check",
    "request_approval": "await_review",
    "tools_approved": "publish",
    "finalize": "understand",
}

# 与 tools/*.py 中 @tool 名称对齐
_READ_TOOLS = frozenset(
    {
        "get_file_content",
        "list_issues",
        "list_pull_requests",
        "list_commits",
        "search_code",
        "compare_branches",
        "review_file",
        "review_pr",
        "security_scan_file",
        "get_last_review",
        "search_codebase",
        "search_code_graph",
        "get_issue",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "update_file",
        "create_branch",
        "suggest_fix",
        "create_issue",
        "add_comment",
    }
)
_PUBLISH_TOOLS = frozenset(
    {
        "create_pr",
        "merge_pr",
    }
)
_CHECK_TOOLS = frozenset(
    {
        "security_scan_file",
        "review_file",
        "review_pr",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> List[Dict[str, Any]]:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(items: List[Dict[str, Any]]) -> None:
    """原子写入（进程内锁 + replace）；跨进程仍可能竞态，生产应迁 Redis/DB。"""
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(items[:_MAX], ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(STORE_PATH.parent), prefix=".task_runs.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _empty_stages() -> List[Dict[str, Any]]:
    return []


def _ensure_stage(run: Dict[str, Any], stage_id: str) -> Dict[str, Any]:
    stages: List[Dict[str, Any]] = run.setdefault("stages", [])
    for s in stages:
        if s.get("id") == stage_id:
            return s
    label = next((d["label"] for d in STAGE_DEFS if d["id"] == stage_id), stage_id)
    stage = {
        "id": stage_id,
        "label": label,
        "status": "pending",  # pending|running|done|failed|skipped
        "started_at": None,
        "ended_at": None,
    }
    # 保持 STAGE_DEFS 顺序
    order = {d["id"]: i for i, d in enumerate(STAGE_DEFS)}
    stages.append(stage)
    stages.sort(key=lambda x: order.get(x.get("id") or "", 99))
    return stage


def create_run(
    *,
    thread_id: str,
    repo: str,
    message: str,
    run_id: str = "",
) -> Dict[str, Any]:
    rid = run_id or uuid.uuid4().hex
    item = {
        "id": rid,
        "thread_id": thread_id,
        "repo": repo,
        "message": (message or "")[:2000],
        "status": "queued",
        "current_stage": None,
        "current_step": "排队中",
        "stages": _empty_stages(),
        "logs": [],
        "error": None,
        "started_at": _now(),
        "ended_at": None,
        "event_seq": 0,
        "tool_evidence": {
            "read": False,
            "write": False,
            "check": False,
            "publish": False,
            "publish_ok": False,
        },
        "external": {
            "pr_url": None,
            "pr_number": None,
            "commit_sha": None,
            "branch": None,
        },
        # 硬刷新 UI 恢复（不等同于成功证据）
        "phase": None,
        "agent_envelopes": [],
        "test_report": {},
        "workspace_diff": {},
        "diff_approved": False,
        "publish_approved": False,
        "cancel_requested": False,
        "cancel_note": None,
        "retry_of": None,
        "updated_at": _now(),
    }
    with _LOCK:
        items = _load()
        items.insert(0, item)
        _save(items)
        _CANCELLED.discard(thread_id)
        _CANCELLED.discard(rid)
    return dict(item)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        for it in _load():
            if it.get("id") == run_id:
                return dict(it)
    return None


def get_run_by_thread(thread_id: str) -> Optional[Dict[str, Any]]:
    """取该 thread 最新一次 run。"""
    with _LOCK:
        for it in _load():
            if it.get("thread_id") == thread_id:
                return dict(it)
    return None


def list_runs(*, repo: str = "", limit: int = 40) -> List[Dict[str, Any]]:
    with _LOCK:
        items = _load()
    out = []
    for it in items:
        if repo and it.get("repo") != repo:
            continue
        out.append(it)
        if len(out) >= limit:
            break
    return out


def request_cancel(thread_id: str, *, reason: str = "user_cancel") -> Optional[Dict[str, Any]]:
    """请求取消：立刻持久化 cancel_requested；流循环见到后停止后续节点。

    注意：正在进行的 DeepSeek/HTTP 调用无法保证立即中断，只能阻止后续步骤。
    """
    note = (
        "已请求取消：将停止后续图节点与工具调度。"
        "若底层 LLM/GitHub 请求已发出，可能需等待该次调用返回后才会完全停下。"
    )
    _CANCELLED.add(thread_id)
    with _LOCK:
        items = _load()
        for i, it in enumerate(items):
            if it.get("thread_id") == thread_id and it.get("status") not in TERMINAL:
                it = {
                    **it,
                    "cancel_requested": True,
                    "cancel_note": note,
                    "status": "cancelled" if it.get("status") == "queued" else it.get("status"),
                    "error": reason,
                    "updated_at": _now(),
                }
                # 若尚未真正开始，直接终态
                if it.get("status") == "cancelled" or it.get("current_stage") is None:
                    it["status"] = "cancelled"
                    it["ended_at"] = it.get("ended_at") or _now()
                    it["current_step"] = "已取消"
                logs = list(it.get("logs") or [])
                logs.append(
                    {
                        "ts": _now(),
                        "level": "warn",
                        "message": note,
                        "seq": int(it.get("event_seq") or 0) + 1,
                    }
                )
                it["logs"] = logs[-200:]
                it["event_seq"] = int(it.get("event_seq") or 0) + 1
                items[i] = it
                _save(items)
                _CANCELLED.add(it["id"])
                return dict(it)
    return get_run_by_thread(thread_id)


def is_cancel_requested(thread_id: str) -> bool:
    if thread_id in _CANCELLED:
        return True
    run = get_run_by_thread(thread_id)
    return bool(run and (run.get("cancel_requested") or run.get("status") == "cancelled"))


def _append_log(run: Dict[str, Any], message: str, level: str = "info") -> None:
    seq = int(run.get("event_seq") or 0) + 1
    run["event_seq"] = seq
    logs = list(run.get("logs") or [])
    logs.append({"ts": _now(), "level": level, "message": message[:1000], "seq": seq})
    run["logs"] = logs[-200:]
    run["updated_at"] = _now()


def _set_stage_running(run: Dict[str, Any], stage_id: str) -> None:
    # 结束上一个 running 阶段
    for s in run.get("stages") or []:
        if s.get("status") == "running" and s.get("id") != stage_id:
            s["status"] = "done"
            s["ended_at"] = s.get("ended_at") or _now()
    stage = _ensure_stage(run, stage_id)
    if stage.get("status") != "done":
        stage["status"] = "running"
        stage["started_at"] = stage.get("started_at") or _now()
    run["current_stage"] = stage_id


def _finish_stage(run: Dict[str, Any], stage_id: str, *, failed: bool = False) -> None:
    stage = _ensure_stage(run, stage_id)
    stage["status"] = "failed" if failed else "done"
    stage["ended_at"] = _now()
    if not stage.get("started_at"):
        stage["started_at"] = stage["ended_at"]


def apply_node_event(
    thread_id: str,
    *,
    node: str,
    status: str,
    label: str = "",
) -> Optional[Dict[str, Any]]:
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if run.get("status") in TERMINAL:
            return run
        # 软终态已落盘后不再被后续节点覆盖
        if run.get("status") in SOFT_TERMINAL and run.get("ended_at"):
            return run

        if thread_id in _CANCELLED or run.get("cancel_requested") or run.get("id") in _CANCELLED:
            run["status"] = "cancelled"
            run["ended_at"] = run.get("ended_at") or _now()
            run["current_step"] = "已取消"
            _append_log(run, f"取消生效，跳过节点 {node}", "warn")
            items[idx] = run
            _save(items)
            return dict(run)

        mapped = _NODE_TO_STATUS.get(node)
        stage_id = _NODE_TO_STAGE.get(node)
        step_label = label or _NODE_LABELS_SAFE(node)

        if status == "enter":
            if mapped and mapped not in TERMINAL:
                # agent 默认 analyzing；有写证据后保持 editing/publishing
                ev = run.get("tool_evidence") or {}
                if node == "agent" and ev.get("write"):
                    run["status"] = "editing"
                elif node == "tools" and ev.get("publish"):
                    run["status"] = "publishing"
                elif node == "tools" and not ev.get("write") and ev.get("read"):
                    run["status"] = "analyzing"
                    stage_id = "analyze_repo"
                elif mapped:
                    run["status"] = mapped
            if stage_id:
                if node == "tools":
                    ev = run.get("tool_evidence") or {}
                    if ev.get("publish"):
                        stage_id = "publish"
                    elif not ev.get("write") and ev.get("read"):
                        stage_id = "analyze_repo"
                _set_stage_running(run, stage_id)
            if node == "coding_deferred":
                run["status"] = "awaiting_coding"
                run["current_step"] = "计划已确认，等待执行"
            else:
                run["current_step"] = step_label
            _append_log(run, f"进入：{step_label}")
        elif status == "done":
            if node == "coding_deferred":
                # 编码未执行：不把 await_coding / edit 标成 done
                for s in run.get("stages") or []:
                    if s.get("id") == "plan_confirm" and s.get("status") == "running":
                        s["status"] = "done"
                        s["ended_at"] = _now()
                    if s.get("id") == "await_coding":
                        s["status"] = "waiting"
                        s["ended_at"] = None
                    if s.get("id") == "edit" and s.get("status") in {"running", "done"}:
                        s["status"] = "skipped"
                        s["ended_at"] = s.get("ended_at") or _now()
                run["status"] = "awaiting_coding"
                run["current_stage"] = "await_coding"
                run["current_step"] = "计划已确认，等待执行"
                _append_log(run, "计划已确认，编码尚未执行（等待执行）")
            elif stage_id and stage_id != "await_review":
                # tools 阶段由工具类型决定，done 时再 finish 当前 stage
                cur = run.get("current_stage")
                if cur:
                    _finish_stage(run, cur)
                run["current_step"] = f"{step_label} · 完成"
                _append_log(run, f"完成：{step_label}")
            else:
                run["current_step"] = f"{step_label} · 完成"
                _append_log(run, f"完成：{step_label}")

        items[idx] = run
        _save(items)
        return dict(run)


def _NODE_LABELS_SAFE(node: str) -> str:
    labels = {
        "start": "启动",
        "classify_intent": "需求理解 / 意图分类",
        "jev_decide": "编排决策",
        "repo_analysis": "仓库分析 Agent",
        "planning_agent": "规划 Agent",
        "await_plan_confirm": "等待确认计划",
        "coding_agent": "工作区编码",
        "await_diff_approve": "等待确认 Diff",
        "testing_agent": "运行测试",
        "await_publish_approve": "等待发布审批",
        "publish_agent": "提交 GitHub / 创建 PR",
        "coding_deferred": "工作区编码（兼容）",
        "planner": "生成修改计划",
        "compact_context": "上下文整理",
        "agent": "推理选工具",
        "tools": "执行工具",
        "guardrail_check": "代码检查 / 护栏",
        "request_approval": "等待发布审批",
        "tools_approved": "提交 GitHub",
        "reject": "已拒绝",
        "finalize": "汇总",
    }
    return labels.get(node, node)


def apply_tool_event(
    thread_id: str,
    *,
    tool: str,
    phase: str,  # start|end
    output: str = "",
    ok_hint: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """根据真实工具调用更新状态。不把 LLM 文本当成功凭证。"""
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if run.get("status") in {"succeeded", "failed", "cancelled", "awaiting_coding"}:
            return run

        if run.get("cancel_requested") or thread_id in _CANCELLED:
            run["status"] = "cancelled"
            run["ended_at"] = run.get("ended_at") or _now()
            run["current_step"] = "已取消"
            items[idx] = run
            _save(items)
            return dict(run)

        name = (tool or "").strip()
        ev = dict(run.get("tool_evidence") or {})
        stage_id = None
        status = None

        if name in _READ_TOOLS or name.startswith("list_") or name.startswith("get_"):
            ev["read"] = True
            stage_id = "analyze_repo"
            status = "analyzing"
        if name in _CHECK_TOOLS:
            ev["check"] = True
            stage_id = "check"
            status = "checking"
        if name in _WRITE_TOOLS or "update_file" in name or "create_branch" in name:
            ev["write"] = True
            stage_id = "edit"
            status = "editing"
        if name in _PUBLISH_TOOLS or "pull_request" in name or name.endswith("_pr"):
            ev["publish"] = True
            stage_id = "publish"
            status = "publishing"

        run["tool_evidence"] = ev
        if status:
            run["status"] = status
        if stage_id and phase == "start":
            _set_stage_running(run, stage_id)
        if phase == "start":
            run["current_step"] = f"工具：{name}"
            _append_log(run, f"工具开始：{name}")
        else:
            out = (output or "")[:800]
            failed = False
            if ok_hint is False:
                failed = True
            elif out:
                low = out.lower()
                if '"ok": false' in low or '"ok":false' in low or low.startswith("error"):
                    failed = True
            if name in _PUBLISH_TOOLS or "pull_request" in name:
                # 仅当输出含真实 PR URL / ok:true 才记 publish_ok
                if ("github.com" in out and "/pull/" in out) or '"ok": true' in out.lower() or '"ok":true' in out.lower():
                    ev["publish_ok"] = True
                    run["tool_evidence"] = ev
                    # 提取 pr url
                    import re

                    m = re.search(r"https://github\.com/[^\s\"']+/pull/\d+", out)
                    if m:
                        ext = dict(run.get("external") or {})
                        ext["pr_url"] = m.group(0)
                        run["external"] = ext
            if stage_id:
                _finish_stage(run, stage_id, failed=failed)
            run["current_step"] = f"工具结束：{name}" + ("（失败）" if failed else "")
            _append_log(run, f"工具结束：{name}" + (f" → {out[:200]}" if out else ""), "error" if failed else "info")

        items[idx] = run
        _save(items)
        return dict(run)


def apply_approval_needed(thread_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if run.get("status") in {"succeeded", "failed", "cancelled", "awaiting_coding"}:
            return run
        run["status"] = "awaiting_approval"
        _set_stage_running(run, "await_review")
        run["current_step"] = "等待人工审核"
        _append_log(run, "进入等待审核（HITL）", "warn")
        items[idx] = run
        _save(items)
        return dict(run)


def apply_diff_approved(
    thread_id: str,
    *,
    final_note: str = "",
) -> Optional[Dict[str, Any]]:
    """Diff 已确认但未发布：软终态，禁止标成远程写成功。"""
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if run.get("status") in TERMINAL and run.get("ended_at"):
            return run
        for s in run.get("stages") or []:
            if s.get("id") in {"edit", "await_review", "plan_confirm"} and s.get("status") == "running":
                s["status"] = "done"
                s["ended_at"] = _now()
            if s.get("id") in {"await_publish", "publish"}:
                s["status"] = "skipped"
                s["ended_at"] = s.get("ended_at") or _now()
        run["status"] = "diff_approved"
        run["diff_approved"] = True
        run["current_stage"] = "await_review"
        run["current_step"] = "Diff 已确认，等待测试/发布"
        run["ended_at"] = _now()
        _append_log(run, final_note or "Diff 已确认；未推送远程", "info")
        items[idx] = run
        _save(items)
        return dict(run)


def patch_ui_snapshot(
    thread_id: str,
    *,
    phase: str = "",
    agent_envelopes: Optional[List[Any]] = None,
    test_report: Optional[Dict[str, Any]] = None,
    workspace_diff: Optional[Dict[str, Any]] = None,
    workspace_files: Optional[List[Any]] = None,
    diff_approved: Optional[bool] = None,
    publish_approved: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """持久化 UI 恢复字段（phase / envelopes / test_report），不改变任务终态语义。"""
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if phase:
            run["phase"] = phase
        if agent_envelopes is not None:
            run["agent_envelopes"] = list(agent_envelopes)[-40:]
        if test_report is not None:
            run["test_report"] = dict(test_report)
        if workspace_diff is not None:
            run["workspace_diff"] = dict(workspace_diff)
        elif workspace_files is not None:
            wd = dict(run.get("workspace_diff") or {})
            wd["files_touched"] = list(workspace_files)
            run["workspace_diff"] = wd
        if diff_approved is not None:
            run["diff_approved"] = bool(diff_approved)
        if publish_approved is not None:
            run["publish_approved"] = bool(publish_approved)
        run["updated_at"] = _now()
        run["event_seq"] = int(run.get("event_seq") or 0) + 1
        items[idx] = run
        _save(items)
        return dict(run)


def record_publish_success(
    thread_id: str,
    *,
    pr_url: Optional[str] = None,
    pr_number: Any = None,
    commit_sha: Optional[str] = None,
    branch: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """记录真实发布证据（PR URL 等），供 apply_terminal(succeeded) 校验。"""
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        ev = dict(run.get("tool_evidence") or {})
        ev["publish"] = True
        if pr_url:
            ev["publish_ok"] = True
        run["tool_evidence"] = ev
        ext = dict(run.get("external") or {})
        if pr_url:
            ext["pr_url"] = pr_url
        if pr_number is not None:
            try:
                ext["pr_number"] = int(pr_number)
            except (TypeError, ValueError):
                ext["pr_number"] = pr_number
        if commit_sha:
            ext["commit_sha"] = commit_sha
        if branch:
            ext["branch"] = branch
        run["external"] = ext
        run["publish_approved"] = True
        run["phase"] = "published"
        run["updated_at"] = _now()
        run["event_seq"] = int(run.get("event_seq") or 0) + 1
        _append_log(
            run,
            f"发布证据：PR={pr_url or '—'} branch={branch or '—'}",
            "info",
        )
        items[idx] = run
        _save(items)
        return dict(run)


def apply_awaiting_coding(
    thread_id: str,
    *,
    final_note: str = "",
) -> Optional[Dict[str, Any]]:
    """计划已确认、编码未执行：软终态，禁止标成 succeeded / edit:done。"""
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if run.get("status") in TERMINAL and run.get("ended_at"):
            return run

        for s in run.get("stages") or []:
            if s.get("id") == "plan_confirm" and s.get("status") != "done":
                s["status"] = "done"
                s["ended_at"] = s.get("ended_at") or _now()
            if s.get("id") == "await_coding":
                s["status"] = "waiting"
                s["ended_at"] = None
            if s.get("id") == "edit":
                s["status"] = "skipped"
                s["ended_at"] = s.get("ended_at") or _now()
            elif s.get("status") == "running" and s.get("id") not in {"await_coding"}:
                s["status"] = "done"
                s["ended_at"] = _now()

        _ensure_stage(run, "await_coding")
        for s in run.get("stages") or []:
            if s.get("id") == "await_coding":
                s["status"] = "waiting"
                s["started_at"] = s.get("started_at") or _now()
                s["ended_at"] = None

        run["status"] = "awaiting_coding"
        run["current_stage"] = "await_coding"
        run["current_step"] = "计划已确认，等待执行"
        run["ended_at"] = _now()  # 停表；非「代码修改成功」
        note = final_note or "计划已确认，等待执行（编码未开始）"
        _append_log(run, note, "info")
        items[idx] = run
        _save(items)
        return dict(run)


def apply_terminal(
    thread_id: str,
    *,
    status: str,
    error: str = "",
    final_note: str = "",
) -> Optional[Dict[str, Any]]:
    """设置终态。succeeded 仅在有工具证据或明确非虚构完成时允许。"""
    if status not in TERMINAL:
        raise ValueError(f"invalid terminal status: {status}")
    with _LOCK:
        items = _load()
        idx = next((i for i, it in enumerate(items) if it.get("thread_id") == thread_id), -1)
        if idx < 0:
            return None
        run = dict(items[idx])
        if run.get("status") in TERMINAL and run.get("ended_at"):
            return run

        if status == "succeeded":
            ev = run.get("tool_evidence") or {}
            # 纯问答：无写/发布也可 succeeded（完成一轮对话）
            # 若曾尝试 publish，必须 publish_ok
            if ev.get("publish") and not ev.get("publish_ok"):
                status = "failed"
                error = error or "检测到发布类工具但未获得真实成功结果（如 PR URL），不能标记成功"
            # 计划确认 ≠ 代码修改成功：无写/发布证据且处于 awaiting_coding 时改软终态
            if (
                status == "succeeded"
                and not ev.get("write")
                and not ev.get("publish_ok")
                and (
                    run.get("status") == "awaiting_coding"
                    or run.get("current_stage") == "await_coding"
                    or any(
                        s.get("id") == "await_coding" and s.get("status") in {"waiting", "running"}
                        for s in (run.get("stages") or [])
                    )
                )
            ):
                items[idx] = run
                _save(items)
                return apply_awaiting_coding(thread_id, final_note=final_note or "计划已确认，等待执行")

        run["status"] = status
        run["ended_at"] = _now()
        if error:
            run["error"] = error[:2000]
        # 关闭 running 阶段（await_coding 不得被标成 done）
        for s in run.get("stages") or []:
            if s.get("id") == "await_coding":
                continue
            if s.get("status") == "running":
                s["status"] = "failed" if status == "failed" else "done"
                s["ended_at"] = _now()
        # 发布失败：即使节点事件已把 publish 标成 done，也要改回 failed，避免 UI「全绿却失败」
        publish_related = False
        if status == "failed":
            phase = str(run.get("phase") or "")
            err_l = (error or "").lower()
            publish_related = (
                phase in {"publish_failed", "publishing", "awaiting_publish"}
                or "github token" in err_l
                or "personal access token" in err_l
                or "create-a-reference" in err_l
                or "publish" in err_l
            )
            if publish_related:
                run["phase"] = "publish_failed"
                run["current_stage"] = "publish"
                for s in run.get("stages") or []:
                    if s.get("id") == "publish":
                        s["status"] = "failed"
                        s["ended_at"] = s.get("ended_at") or _now()
        if status == "succeeded":
            run["current_step"] = "已完成"
        elif status == "cancelled":
            run["current_step"] = "已取消"
        elif publish_related:
            run["current_step"] = "发布失败"
        else:
            run["current_step"] = "失败"
        if final_note:
            _append_log(run, final_note, "error" if status == "failed" else "info")
        else:
            _append_log(run, f"任务终态：{status}", "error" if status == "failed" else "info")
        items[idx] = run
        _save(items)
        return dict(run)


def mark_cancelled_complete(thread_id: str) -> Optional[Dict[str, Any]]:
    return apply_terminal(
        thread_id,
        status="cancelled",
        error="cancelled",
        final_note="任务已取消（后续步骤未执行）",
    )


def public_view(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not run:
        return None
    started = run.get("started_at")
    ended = run.get("ended_at")
    elapsed_ms = None
    try:
        if started:
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat((ended or _now()).replace("Z", "+00:00"))
            elapsed_ms = max(0, int((t1 - t0).total_seconds() * 1000))
    except Exception:  # noqa: BLE001
        elapsed_ms = None
    return {
        "id": run.get("id"),
        "thread_id": run.get("thread_id"),
        "repo": run.get("repo"),
        "message": run.get("message"),
        "status": run.get("status"),
        "current_stage": run.get("current_stage"),
        "current_step": run.get("current_step"),
        "stages": run.get("stages") or [],
        "logs": run.get("logs") or [],
        "error": run.get("error"),
        "started_at": started,
        "ended_at": ended,
        "elapsed_ms": elapsed_ms,
        "event_seq": run.get("event_seq") or 0,
        "tool_evidence": run.get("tool_evidence") or {},
        "external": run.get("external") or {},
        "phase": run.get("phase"),
        "agent_envelopes": run.get("agent_envelopes") or [],
        "test_report": run.get("test_report") or {},
        "workspace_diff": run.get("workspace_diff") or {},
        "diff_approved": bool(run.get("diff_approved")),
        "publish_approved": bool(run.get("publish_approved")),
        "cancel_requested": bool(run.get("cancel_requested")),
        "cancel_note": run.get("cancel_note"),
        "retry_of": run.get("retry_of"),
        "updated_at": run.get("updated_at"),
    }


def can_retry(run: Optional[Dict[str, Any]]) -> tuple[bool, str]:
    if not run:
        return False, "任务不存在"
    st = run.get("status")
    if st == "succeeded":
        return False, "任务已成功完成，禁止重试以免重复提交外部操作"
    if st == "awaiting_coding":
        return False, "计划已确认，等待执行编码；请新开任务，勿整任务重试"
    if st == "diff_approved":
        return False, "Diff 已确认待发布；请走 Phase 3 发布流程，勿整任务重试"
    if st not in {"failed", "cancelled"}:
        return False, f"当前状态 {st} 不可重试"
    # 若已成功创建过 PR，也禁止「整任务重试」自动再跑发布
    if (run.get("tool_evidence") or {}).get("publish_ok") or (run.get("external") or {}).get("pr_url"):
        return False, "已存在成功的 PR/发布记录，请新开任务继续修改，避免重复提交"
    return True, ""


def create_retry_run(prev: Dict[str, Any]) -> Dict[str, Any]:
    ok, reason = can_retry(prev)
    if not ok:
        raise ValueError(reason)
    # 新 thread，避免复用已污染的检查点重复执行 create_pr
    new_thread = uuid.uuid4().hex
    run = create_run(
        thread_id=new_thread,
        repo=str(prev.get("repo") or ""),
        message=str(prev.get("message") or ""),
    )
    with _LOCK:
        items = _load()
        for i, it in enumerate(items):
            if it.get("id") == run["id"]:
                it["retry_of"] = prev.get("id")
                items[i] = it
                _save(items)
                return dict(it)
    return run
