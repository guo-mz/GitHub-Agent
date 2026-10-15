"""条件边路由函数 —— 决定状态机下一步。"""

from __future__ import annotations

from typing import Literal

from app.agent.state import AgentState


def route_after_classify(state: AgentState) -> Literal["finalize", "jev_decide"]:
    """越界快路径：分类节点已写入 final_answer 时直达收尾，跳过 Jev/上下文/Agent。"""
    intent = state.get("intent") or {}
    if intent.get("out_of_scope") and (state.get("final_answer") or "").strip():
        return "finalize"
    return "jev_decide"


def route_after_jev(
    state: AgentState,
) -> Literal["repo_analysis", "compact_context"]:
    """write/review 或 Jev 要求规划 → 多 Agent 分析链路；纯 qa 走旧 ReAct。"""
    jev = state.get("jev") or {}
    answers = jev.get("answers") or {}
    need = answers.get("need_planner") or {}
    need_v = need.get("value") if isinstance(need, dict) else None
    try:
        need_plan = float(need_v) >= 0.55 if need_v is not None else False
    except (TypeError, ValueError):
        need_plan = bool(need_v)

    task = (state.get("task_type") or "qa").lower()
    if need_plan or task in {"read", "write", "review"}:
        return "repo_analysis"
    return "compact_context"


def route_after_plan_confirm(
    state: AgentState,
) -> Literal["coding_agent", "finalize"]:
    dec = (state.get("approval_decision") or "").lower()
    if dec == "approve" and state.get("plan_confirmed"):
        return "coding_agent"
    return "finalize"


def route_after_coding(
    state: AgentState,
) -> Literal["await_diff_approve", "finalize"]:
    """编码成功且有真实 Diff → Diff 审批；否则收尾。"""
    if state.get("error"):
        return "finalize"
    wd = state.get("workspace_diff") or {}
    if wd.get("combined_diff") and (wd.get("diffs") or wd.get("files_touched")):
        return "await_diff_approve"
    return "finalize"


MAX_REPAIR = 2


def route_after_diff_approve(
    state: AgentState,
) -> Literal["testing_agent", "finalize"]:
    if state.get("diff_approved"):
        return "testing_agent"
    return "finalize"


def route_after_testing(
    state: AgentState,
) -> Literal["coding_agent", "await_publish_approve"]:
    """测试失败且未超修复上限 → 回编码；not_run / 通过 → 发布审批。"""
    report = state.get("test_report") or {}
    failed = bool(report.get("failed"))
    repair = int(state.get("repair_count") or 0)
    if failed and repair < MAX_REPAIR:
        return "coding_agent"
    return "await_publish_approve"


def route_after_publish_approve(
    state: AgentState,
) -> Literal["publish_agent", "finalize"]:
    if state.get("publish_approved"):
        return "publish_agent"
    return "finalize"


def route_after_agent(state: AgentState) -> Literal["tools", "finalize"]:
    messages = state.get("messages") or []
    if not messages:
        return "finalize"
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if tool_calls:
        return "tools"
    return "finalize"


def route_after_guardrail(
    state: AgentState,
) -> Literal["request_approval", "compact_context", "reject"]:
    if state.get("error") and state.get("need_approval") is False and "护栏拦截" in (state.get("error") or ""):
        return "reject"
    if state.get("need_approval"):
        return "request_approval"
    return "compact_context"


def route_after_approval(state: AgentState) -> Literal["tools_approved", "reject"]:
    decision = (state.get("approval_decision") or "").lower()
    if decision == "approve":
        return "tools_approved"
    return "reject"


def route_after_tools(
    state: AgentState,
) -> Literal["guardrail_check", "planner", "compact_context"]:
    if state.get("conflict"):
        retry = int(state.get("retry_count") or 0)
        if retry < 3:
            return "planner"
        return "compact_context"
    return "guardrail_check"
