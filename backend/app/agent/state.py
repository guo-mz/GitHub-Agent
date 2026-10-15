"""AgentState + Reducer —— 节点只返回增量 dict，禁止原地修改。

兼容旧 checkpoint：新增字段均有默认值，缺失时按空处理。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    repo: str
    task_type: str
    intent: dict
    jev: dict
    plan: list  # 旧版步骤列表（冲突重试用）
    changed_files: Annotated[list, operator.add]
    retry_count: int
    need_approval: bool
    approval_payload: dict
    approval_decision: str
    error: str
    conflict: bool
    last_tool_results: list
    final_answer: str
    trace_id: str
    context_summary: str
    context_meta: dict
    # Multi-Agent Phase 1
    phase: str  # analyzing|planning|awaiting_plan_confirm|awaiting_coding|...
    analysis_report: dict
    plan_document: dict
    plan_confirmed: bool
    approval_kind: str  # plan_confirm | diff_approve | publish | ""
    agent_envelopes: list  # List[AgentEnvelope dict]
    coding_blocked_reason: str
    # Phase 2
    workspace_id: str
    workspace_diff: dict  # snapshot_to_dict
    diff_approved: bool
    approved_content_hash: str
    # Phase 3
    repair_count: int
    test_report: dict
    publish_approved: bool
    publish_result: dict


def initial_state(repo: str, trace_id: str = "") -> Dict[str, Any]:
    return {
        "repo": repo,
        "task_type": "",
        "intent": {},
        "jev": {},
        "plan": [],
        "changed_files": [],
        "retry_count": 0,
        "need_approval": False,
        "approval_payload": {},
        "approval_decision": "",
        "error": "",
        "conflict": False,
        "last_tool_results": [],
        "final_answer": "",
        "trace_id": trace_id,
        "context_summary": "",
        "context_meta": {},
        "phase": "queued",
        "analysis_report": {},
        "plan_document": {},
        "plan_confirmed": False,
        "approval_kind": "",
        "agent_envelopes": [],
        "coding_blocked_reason": "",
        "workspace_id": "",
        "workspace_diff": {},
        "diff_approved": False,
        "approved_content_hash": "",
        "repair_count": 0,
        "test_report": {},
        "publish_approved": False,
        "publish_result": {},
    }
