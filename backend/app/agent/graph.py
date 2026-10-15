"""LangGraph 状态机装配：节点 + 条件边 + Checkpointer + interrupt。

面试点：
- compile(checkpointer=...) 才能 interrupt/resume
- recursion_limit 在 invoke/astream config 里设，防死循环
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Optional

from langgraph.graph import END, START, StateGraph

from app.agent import edges, nodes
from app.agent.state import AgentState, initial_state
from app.config import get_settings
from app.core.checkpointer import agent_run_config, get_checkpointer
from app.core.retry import is_retryable
from app.core.tracing import agent_trace, span_event

logger = logging.getLogger(__name__)

_compiled = None

# 只把这些节点的模型输出推给前端（classify/planner 的 JSON 不展示）
_USER_VISIBLE_LLM_NODES = {"agent", "finalize"}

# 推给前端的状态机节点时间线（面试可视化）
_GRAPH_NODES = {
    "classify_intent",
    "jev_decide",
    "repo_analysis",
    "planning_agent",
    "await_plan_confirm",
    "coding_agent",
    "await_diff_approve",
    "testing_agent",
    "await_publish_approve",
    "publish_agent",
    "coding_deferred",
    "planner",
    "compact_context",
    "agent",
    "tools",
    "guardrail_check",
    "request_approval",
    "tools_approved",
    "reject",
    "finalize",
}

_NODE_LABELS = {
    "start": "启动",
    "classify_intent": "意图分类",
    "jev_decide": "编排决策",
    "repo_analysis": "仓库分析 Agent",
    "planning_agent": "规划 Agent",
    "await_plan_confirm": "等待确认计划",
    "coding_agent": "编码 Agent（工作区）",
    "await_diff_approve": "等待确认 Diff",
    "testing_agent": "Testing Agent",
    "await_publish_approve": "等待发布审批",
    "publish_agent": "Publish Agent",
    "coding_deferred": "编码 Agent（兼容）",
    "planner": "规划步骤（冲突重试）",
    "compact_context": "上下文压缩",
    "agent": "推理 / 选工具",
    "tools": "执行工具",
    "guardrail_check": "护栏校验",
    "request_approval": "等待发布审批",
    "tools_approved": "审批后执行",
    "reject": "已拒绝",
    "finalize": "汇总回答",
}


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("classify_intent", nodes.classify_intent)
    g.add_node("jev_decide", nodes.jev_decide)
    g.add_node("repo_analysis", nodes.repo_analysis_node)
    g.add_node("planning_agent", nodes.planning_agent_node)
    g.add_node("await_plan_confirm", nodes.await_plan_confirm_node)
    g.add_node("coding_agent", nodes.coding_agent_node)
    g.add_node("await_diff_approve", nodes.await_diff_approve_node)
    g.add_node("testing_agent", nodes.testing_agent_node)
    g.add_node("await_publish_approve", nodes.await_publish_approve_node)
    g.add_node("publish_agent", nodes.publish_agent_node)
    g.add_node("coding_deferred", nodes.coding_deferred_node)  # 旧检查点兼容
    # 节点名 planner（不能用 plan：与 state key 冲突）—— 仅冲突重试
    g.add_node("planner", nodes.plan_node)
    g.add_node("compact_context", nodes.compact_context_node)
    g.add_node("agent", nodes.agent_node)
    g.add_node("tools", nodes.tools_node)
    g.add_node("guardrail_check", nodes.guardrail_check)
    g.add_node("request_approval", nodes.request_approval)
    g.add_node("tools_approved", nodes.tools_approved_node)
    g.add_node("reject", nodes.reject_node)
    g.add_node("finalize", nodes.finalize)

    g.add_edge(START, "classify_intent")
    g.add_conditional_edges(
        "classify_intent",
        edges.route_after_classify,
        {"jev_decide": "jev_decide", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "jev_decide",
        edges.route_after_jev,
        {"repo_analysis": "repo_analysis", "compact_context": "compact_context"},
    )
    g.add_edge("repo_analysis", "planning_agent")
    g.add_edge("planning_agent", "await_plan_confirm")
    g.add_conditional_edges(
        "await_plan_confirm",
        edges.route_after_plan_confirm,
        {"coding_agent": "coding_agent", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "coding_agent",
        edges.route_after_coding,
        {"await_diff_approve": "await_diff_approve", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "await_diff_approve",
        edges.route_after_diff_approve,
        {"testing_agent": "testing_agent", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "testing_agent",
        edges.route_after_testing,
        {
            "coding_agent": "coding_agent",
            "await_publish_approve": "await_publish_approve",
        },
    )
    g.add_conditional_edges(
        "await_publish_approve",
        edges.route_after_publish_approve,
        {"publish_agent": "publish_agent", "finalize": "finalize"},
    )
    g.add_edge("publish_agent", "finalize")
    g.add_edge("coding_deferred", "finalize")  # 旧边兼容：直接收尾
    # 冲突重试：旧 planner → 压缩 → agent（仍受 plan_confirmed 写门闩约束）
    g.add_edge("planner", "compact_context")
    g.add_edge("compact_context", "agent")
    g.add_conditional_edges(
        "agent",
        edges.route_after_agent,
        {"tools": "tools", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "tools",
        edges.route_after_tools,
        {
            "guardrail_check": "guardrail_check",
            "planner": "planner",
            "compact_context": "compact_context",
        },
    )
    g.add_conditional_edges(
        "guardrail_check",
        edges.route_after_guardrail,
        {
            "request_approval": "request_approval",
            "compact_context": "compact_context",
            "reject": "reject",
        },
    )
    g.add_conditional_edges(
        "request_approval",
        edges.route_after_approval,
        {
            "tools_approved": "tools_approved",
            "reject": "reject",
        },
    )
    g.add_edge("tools_approved", "compact_context")
    g.add_edge("reject", "finalize")
    g.add_edge("finalize", END)
    return g


async def get_compiled_graph():
    global _compiled
    # 开发期允许热更新图结构（reload 后重建）
    if _compiled is not None:
        return _compiled
    checkpointer = await get_checkpointer()
    _compiled = build_graph().compile(checkpointer=checkpointer)
    return _compiled


def reset_compiled_graph() -> None:
    global _compiled
    _compiled = None


def _event_node(event: dict) -> str:
    meta = event.get("metadata") or {}
    return str(meta.get("langgraph_node") or event.get("name") or "")


def _message_text(msg: Any) -> str:
    if msg is None:
        return ""
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
        return "".join(parts)
    return str(content)


def _intent_from_classify_output(out: Any) -> Optional[Dict[str, Any]]:
    """从 classify 节点输出里拿出 intent（dict 或 [classify] 文本）。"""
    if isinstance(out, dict):
        if isinstance(out.get("intent"), dict):
            return out["intent"]
        for m in reversed(out.get("messages") or []):
            text = _message_text(m)
            parsed = _parse_classify_line(text)
            if parsed:
                return parsed
        return None
    if isinstance(out, list):
        for item in reversed(out):
            got = _intent_from_classify_output(item)
            if got:
                return got
    text = _message_text(out)
    return _parse_classify_line(text)


def _parse_classify_line(text: str) -> Optional[Dict[str, Any]]:
    if not text or "[classify]" not in text:
        return None
    # [classify] 意图=review；置信=0.90；来源=rules；文件=README.md；PR=#12
    body = text.split("[classify]", 1)[-1].strip()
    parts = [p.strip() for p in body.split("；") if p.strip()]
    kv: Dict[str, str] = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.strip()] = v.strip()
    task = kv.get("意图") or kv.get("task_type") or ""
    if not task:
        return None
    conf = 0.0
    try:
        conf = float(kv.get("置信") or kv.get("confidence") or 0)
    except ValueError:
        pass
    slots: Dict[str, Any] = {}
    if kv.get("文件"):
        slots["path"] = kv["文件"]
    if kv.get("PR") or kv.get("PR#"):
        num = (kv.get("PR") or kv.get("PR#") or "").lstrip("#")
        if num.isdigit():
            slots["pr_number"] = int(num)
    if kv.get("分支"):
        slots["branch"] = kv["分支"]
    return {
        "task_type": task,
        "confidence": conf,
        "source": kv.get("来源") or "rules",
        "reason": "",
        "slots": slots,
        "hits": [],
    }


def _pick_final_answer(values: dict) -> str:
    answer = (values.get("final_answer") or "").strip()
    if answer:
        return answer
    messages = values.get("messages") or []
    for m in reversed(messages):
        text = _message_text(m).strip()
        if not text:
            continue
        # 跳过内部标记
        if text.startswith("[classify]") or text.startswith("[plan]") or text.startswith("[approval]"):
            continue
        if text.startswith("{") and "task_type" in text:
            continue
        return text
    return ""


def _task_status_event(thread_id: str, trace_id: str) -> Optional[Dict[str, Any]]:
    from app.services import task_run_service as trs

    view = trs.public_view(trs.get_run_by_thread(thread_id))
    if not view:
        return None
    return {
        "event": "task_status",
        "node": "task",
        "data": view,
        "thread_id": thread_id,
        "trace_id": trace_id,
        "seq": view.get("event_seq"),
    }


async def stream_agent(
    message: str,
    repo: str,
    thread_id: str,
    resume: bool = False,
    approval_decision: Optional[Dict[str, Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """SSE 友好的事件流：thought / action / observation / approval_needed / done / task_status。"""
    from langgraph.types import Command

    from app.services import task_run_service as trs

    graph = await get_compiled_graph()
    settings = get_settings()
    config = agent_run_config(thread_id, settings.agent_recursion_limit)
    trace_id = thread_id
    settings_repo = (settings.default_repo or "").strip()
    if not repo or repo in {"owner/repo", "owner/name"}:
        repo = settings_repo or "guo-mz/-"

    from app.services.llm_usage.context import llm_call_context

    # 若尚无任务记录（resume 场景），先补一条，便于用量写入 task_id
    run = trs.get_run_by_thread(thread_id)
    if not run:
        run = trs.create_run(thread_id=thread_id, repo=repo, message=message or "(resume)")
    task_id = str((run or {}).get("id") or "")

    with (
        agent_trace("github_agent_run", trace_id=trace_id, metadata={"repo": repo}) as ctx,
        llm_call_context(trace_id=trace_id, thread_id=thread_id, task_id=task_id),
    ):
        if resume and approval_decision is not None:
            inp: Any = Command(resume=approval_decision)
        elif resume:
            inp = Command(resume={"decision": "approve"})
        else:
            from langchain_core.messages import HumanMessage

            base = initial_state(repo, trace_id=trace_id)
            base["messages"] = [HumanMessage(content=message)]
            inp = base

        trs.apply_node_event(thread_id, node="start", status="enter", label=_NODE_LABELS["start"])
        yield {
            "event": "node",
            "node": "start",
            "data": {
                "repo": repo,
                "thread_id": thread_id,
                "status": "enter",
                "label": _NODE_LABELS["start"],
            },
            "thread_id": thread_id,
            "trace_id": trace_id,
        }
        ts0 = _task_status_event(thread_id, trace_id)
        if ts0:
            yield ts0

        stream_attempts = max(1, int(settings.agent_stream_max_retries) + 1)
        for stream_try in range(1, stream_attempts + 1):
            progressed = False
            try:
                async for event in graph.astream_events(inp, config=config, version="v2"):
                    if trs.is_cancel_requested(thread_id):
                        trs.mark_cancelled_complete(thread_id)
                        ts = _task_status_event(thread_id, trace_id)
                        if ts:
                            yield ts
                        yield {
                            "event": "error",
                            "node": "graph",
                            "data": {
                                "message": "任务已取消",
                                "cancelled": True,
                                "note": (
                                    "已停止后续节点调度。若当时有 DeepSeek/GitHub 请求在途，"
                                    "该次调用可能仍会跑完，但结果不会继续驱动状态机。"
                                ),
                            },
                            "thread_id": thread_id,
                            "trace_id": trace_id,
                        }
                        return

                    kind = event.get("event")
                    name = event.get("name") or ""
                    data = event.get("data") or {}
                    node = _event_node(event)

                    # 状态机节点进出 → 前端时间线
                    if name in _GRAPH_NODES:
                        if kind == "on_chain_start":
                            progressed = True
                            trs.apply_node_event(
                                thread_id,
                                node=name,
                                status="enter",
                                label=_NODE_LABELS.get(name, name),
                            )
                            yield {
                                "event": "node",
                                "node": name,
                                "data": {
                                    "status": "enter",
                                    "label": _NODE_LABELS.get(name, name),
                                },
                                "thread_id": thread_id,
                                "trace_id": trace_id,
                            }
                            ts = _task_status_event(thread_id, trace_id)
                            if ts:
                                yield ts
                        elif kind == "on_chain_end":
                            label = _NODE_LABELS.get(name, name)
                            intent_payload = None
                            jev_payload = None
                            if name == "classify_intent":
                                out = data.get("output")
                                intent_payload = _intent_from_classify_output(out)
                                if intent_payload:
                                    from app.services.intent import IntentResult

                                    label = IntentResult(
                                        task_type=str(intent_payload.get("task_type") or "qa"),
                                        confidence=float(intent_payload.get("confidence") or 0),
                                        source=str(intent_payload.get("source") or "rules"),
                                        reason=str(intent_payload.get("reason") or ""),
                                        slots=intent_payload.get("slots") or {},
                                        hits=intent_payload.get("hits") or [],
                                    ).timeline_label()
                            elif name == "jev_decide":
                                out = data.get("output") or {}
                                if isinstance(out, dict):
                                    jev_payload = out.get("jev")
                                    if isinstance(jev_payload, dict):
                                        from app.services.jev_service import JevAnswer, JevBundle, timeline_label

                                        answers = {}
                                        for k, v in (jev_payload.get("answers") or {}).items():
                                            if isinstance(v, dict):
                                                answers[k] = JevAnswer(
                                                    field=str(v.get("field") or k),
                                                    type=str(v.get("type") or "choice"),
                                                    value=v.get("value"),
                                                    confidence=v.get("confidence"),
                                                    probabilities=v.get("probabilities"),
                                                )
                                        label = timeline_label(
                                            JevBundle(
                                                mode=str(jev_payload.get("mode") or "local"),
                                                model=str(jev_payload.get("model") or "jev"),
                                                answers=answers,
                                                requires_review=bool(jev_payload.get("requires_review")),
                                            )
                                        )
                            elif name == "compact_context":
                                out = data.get("output") or {}
                                if isinstance(out, dict):
                                    meta = out.get("context_meta") or {}
                                    if isinstance(meta, dict) and meta:
                                        from app.services.context_engine import ContextMeta

                                        label = ContextMeta(
                                            raw_messages=int(meta.get("raw_messages") or 0),
                                            kept_messages=int(meta.get("kept_messages") or 0),
                                            dropped_messages=int(meta.get("dropped_messages") or 0),
                                            approx_tokens_before=int(
                                                meta.get("approx_tokens_before") or 0
                                            ),
                                            approx_tokens_after=int(
                                                meta.get("approx_tokens_after") or 0
                                            ),
                                            tool_truncated=int(meta.get("tool_truncated") or 0),
                                            summarized=bool(meta.get("summarized")),
                                            mode=str(meta.get("mode") or "passthrough"),
                                        ).timeline_label()
                            trs.apply_node_event(
                                thread_id, node=name, status="done", label=label
                            )
                            yield {
                                "event": "node",
                                "node": name,
                                "data": {
                                    "status": "done",
                                    "label": label,
                                    "intent": intent_payload,
                                    "jev": jev_payload,
                                },
                                "thread_id": thread_id,
                                "trace_id": trace_id,
                            }
                            ts = _task_status_event(thread_id, trace_id)
                            if ts:
                                yield ts

                    if kind == "on_chain_start" and name in _USER_VISIBLE_LLM_NODES | {
                        "tools",
                        "request_approval",
                        "finalize",
                    }:
                        span_event(ctx, name, input_data=str(data.get("input"))[:500])

                    # 仅 agent/finalize 的流式字推给前端
                    if kind == "on_chat_model_stream" and node in _USER_VISIBLE_LLM_NODES:
                        chunk = data.get("chunk")
                        content = getattr(chunk, "content", None) if chunk is not None else None
                        if content:
                            yield {
                                "event": "thought",
                                "node": node,
                                "data": {"delta": content},
                                "thread_id": thread_id,
                                "trace_id": trace_id,
                            }

                    # 一轮模型输出结束：用完整文本覆盖（防止碎片乱序）
                    if kind == "on_chat_model_end" and node in _USER_VISIBLE_LLM_NODES:
                        output = data.get("output")
                        text = _message_text(output).strip()
                        # 有 tool_calls 时 content 可能为空，跳过
                        tool_calls = getattr(output, "tool_calls", None) or []
                        if text and not tool_calls:
                            yield {
                                "event": "thought",
                                "node": node,
                                "data": {"replace": text},
                                "thread_id": thread_id,
                                "trace_id": trace_id,
                            }

                    if kind == "on_tool_start":
                        trs.apply_tool_event(thread_id, tool=name, phase="start")
                        yield {
                            "event": "action",
                            "node": "tools",
                            "data": {
                                "tool": name,
                                "input": data.get("input"),
                            },
                            "thread_id": thread_id,
                            "trace_id": trace_id,
                        }
                        ts = _task_status_event(thread_id, trace_id)
                        if ts:
                            yield ts

                    if kind == "on_tool_end":
                        output = data.get("output")
                        content = getattr(output, "content", output)
                        out_str = str(content)[:4000]
                        trs.apply_tool_event(
                            thread_id, tool=name, phase="end", output=out_str
                        )
                        yield {
                            "event": "observation",
                            "node": "tools",
                            "data": {
                                "tool": name,
                                "output": out_str,
                            },
                            "thread_id": thread_id,
                            "trace_id": trace_id,
                        }
                        ts = _task_status_event(thread_id, trace_id)
                        if ts:
                            yield ts

                if trs.is_cancel_requested(thread_id):
                    trs.mark_cancelled_complete(thread_id)
                    ts = _task_status_event(thread_id, trace_id)
                    if ts:
                        yield ts
                    return

                state = await graph.aget_state(config)
                if state and state.next:
                    interrupt_payload = None
                    for task in state.tasks or []:
                        inter = getattr(task, "interrupts", None) or []
                        if inter:
                            interrupt_payload = inter[0].value if hasattr(inter[0], "value") else inter[0]
                            break
                    next_nodes = list(state.next or ())
                    if (
                        interrupt_payload
                        or "request_approval" in next_nodes
                        or "await_plan_confirm" in next_nodes
                        or "await_diff_approve" in next_nodes
                        or "await_publish_approve" in next_nodes
                    ):
                        values = state.values or {}
                        payload = values.get("approval_payload") or (interrupt_payload or {})
                        if isinstance(interrupt_payload, dict) and "payload" in interrupt_payload:
                            payload = interrupt_payload.get("payload") or payload
                        kind = ""
                        if isinstance(interrupt_payload, dict):
                            kind = str(
                                interrupt_payload.get("approval_kind")
                                or (interrupt_payload.get("payload") or {}).get("approval_kind")
                                or ""
                            )
                        if not kind and isinstance(payload, dict):
                            kind = str(payload.get("approval_kind") or "")
                        if "await_plan_confirm" in next_nodes or kind == "plan_confirm":
                            kind = "plan_confirm"
                            event_name = "plan_confirm_needed"
                            node_name = "await_plan_confirm"
                            trs.apply_node_event(
                                thread_id,
                                node="await_plan_confirm",
                                status="enter",
                                label="等待确认计划",
                            )
                        elif "await_diff_approve" in next_nodes or kind == "diff_approve":
                            kind = "diff_approve"
                            event_name = "diff_approve_needed"
                            node_name = "await_diff_approve"
                            trs.apply_node_event(
                                thread_id,
                                node="await_diff_approve",
                                status="enter",
                                label="等待确认 Diff",
                            )
                            trs.apply_approval_needed(thread_id)
                        elif "await_publish_approve" in next_nodes or kind == "publish":
                            kind = "publish"
                            event_name = "approval_needed"
                            node_name = "await_publish_approve"
                            trs.apply_node_event(
                                thread_id,
                                node="await_publish_approve",
                                status="enter",
                                label="等待发布审批",
                            )
                            trs.apply_approval_needed(thread_id)
                        else:
                            kind = kind or "publish"
                            event_name = "approval_needed"
                            node_name = "request_approval"
                            trs.apply_approval_needed(thread_id)
                        # 硬刷新：审批中断点也落盘 envelopes / phase
                        trs.patch_ui_snapshot(
                            thread_id,
                            phase=str((values or {}).get("phase") or kind),
                            agent_envelopes=(values or {}).get("agent_envelopes") or [],
                            test_report=(values or {}).get("test_report") or {},
                            workspace_diff={
                                "files_touched": (
                                    ((values or {}).get("workspace_diff") or {}).get(
                                        "files_touched"
                                    )
                                ),
                                "stats": ((values or {}).get("workspace_diff") or {}).get(
                                    "stats"
                                ),
                                "combined_diff": (
                                    (
                                        ((values or {}).get("workspace_diff") or {}).get(
                                            "combined_diff"
                                        )
                                        or ""
                                    )[:20000]
                                ),
                            },
                            diff_approved=bool((values or {}).get("diff_approved")),
                            publish_approved=bool((values or {}).get("publish_approved")),
                        )
                        yield {
                            "event": event_name,
                            "node": node_name,
                            "data": {
                                "thread_id": thread_id,
                                "task_id": thread_id,
                                "approval_kind": kind,
                                "payload": payload,
                                "agent_envelopes": (values or {}).get("agent_envelopes") or [],
                                "test_report": (values or {}).get("test_report") or {},
                                "next": next_nodes,
                            },
                            "thread_id": thread_id,
                            "trace_id": trace_id,
                        }
                        ts = _task_status_event(thread_id, trace_id)
                        if ts:
                            yield ts
                        return

                final_state = await graph.aget_state(config)
                values = (final_state.values if final_state else {}) or {}
                # 终态：不以 final_answer 文本作为成功凭证；由工具证据决定
                run = trs.get_run_by_thread(thread_id)
                if trs.is_cancel_requested(thread_id):
                    trs.mark_cancelled_complete(thread_id)
                elif run and run.get("status") == "awaiting_approval":
                    pass
                else:
                    err = (values.get("error") or "") if isinstance(values, dict) else ""
                    dec = str((values or {}).get("approval_decision") or "").lower()
                    phase = str((values or {}).get("phase") or "")
                    if dec == "reject" or phase == "cancelled":
                        kind = str((values or {}).get("approval_kind") or "")
                        err_code = {
                            "diff_approve": "diff_rejected",
                            "publish": "publish_rejected",
                        }.get(kind, "plan_rejected")
                        trs.apply_terminal(
                            thread_id,
                            status="cancelled",
                            error=err_code,
                            final_note="用户拒绝计划/Diff/发布，远程仓库未写入",
                        )
                    elif err or phase == "publish_failed":
                        trs.apply_terminal(
                            thread_id,
                            status="failed",
                            error=str(err or "publish_failed"),
                            final_note="状态含 error",
                        )
                    elif phase == "published":
                        pr = (values or {}).get("publish_result") or {}
                        trs.record_publish_success(
                            thread_id,
                            pr_url=pr.get("pr_url") or (pr.get("pr") or {}).get("html_url"),
                            pr_number=pr.get("pr_number") or (pr.get("pr") or {}).get("number"),
                            commit_sha=pr.get("commit_sha"),
                            branch=pr.get("branch"),
                        )
                        trs.apply_terminal(
                            thread_id,
                            status="succeeded",
                            final_note="发布成功（含真实 PR URL 证据）",
                        )
                    elif phase in {"awaiting_coding", "awaiting_diff_approve"}:
                        trs.apply_awaiting_coding(
                            thread_id,
                            final_note="计划已确认，等待编码或 Diff 审批",
                        )
                    elif phase == "diff_approved":
                        # 兼容旧检查点：停在 Diff 已确认
                        trs.apply_diff_approved(
                            thread_id,
                            final_note="Diff 已确认；等待后续测试/发布",
                        )
                    else:
                        trs.apply_terminal(
                            thread_id,
                            status="succeeded",
                            final_note="图执行结束（成功基于工具证据校验，非模型口述）",
                        )
                    # 硬刷新恢复：持久化 phase / envelopes / test_report
                    trs.patch_ui_snapshot(
                        thread_id,
                        phase=phase,
                        agent_envelopes=(values or {}).get("agent_envelopes") or [],
                        test_report=(values or {}).get("test_report") or {},
                        workspace_diff={
                            "files_touched": ((values or {}).get("workspace_diff") or {}).get(
                                "files_touched"
                            ),
                            "stats": ((values or {}).get("workspace_diff") or {}).get("stats"),
                            "combined_diff": (
                                ((values or {}).get("workspace_diff") or {}).get("combined_diff")
                                or ""
                            )[:20000],
                        },
                        diff_approved=bool((values or {}).get("diff_approved")),
                        publish_approved=bool((values or {}).get("publish_approved")),
                    )
                yield {
                    "event": "done",
                    "node": "finalize",
                    "data": {
                        "final_answer": _pick_final_answer(values),
                        "changed_files": values.get("changed_files") or [],
                        "task_type": values.get("task_type"),
                        "phase": values.get("phase"),
                        "plan_confirmed": bool(values.get("plan_confirmed")),
                        "diff_approved": bool(values.get("diff_approved")),
                        "publish_approved": bool(values.get("publish_approved")),
                        "test_report": values.get("test_report") or {},
                        "publish_result": values.get("publish_result") or {},
                        "workspace_diff": values.get("workspace_diff") or {},
                        "agent_envelopes": values.get("agent_envelopes") or [],
                    },
                    "thread_id": thread_id,
                    "trace_id": trace_id,
                }
                ts = _task_status_event(thread_id, trace_id)
                if ts:
                    yield ts
                return
            except Exception as exc:  # noqa: BLE001
                # 仅冷启动瞬时故障整轮重试；已推进的节点交给节点内重试，避免检查点错乱
                can_retry = (
                    stream_try < stream_attempts
                    and not progressed
                    and not resume
                    and is_retryable(exc)
                )
                if can_retry:
                    logger.warning(
                        "stream_agent cold-start retry %s/%s: %s",
                        stream_try,
                        stream_attempts,
                        exc,
                    )
                    yield {
                        "event": "retry",
                        "node": "graph",
                        "data": {
                            "attempt": stream_try,
                            "max_attempts": stream_attempts,
                            "message": str(exc) or type(exc).__name__,
                            "label": f"瞬时故障，整轮重试 {stream_try}/{stream_attempts}",
                        },
                        "thread_id": thread_id,
                        "trace_id": trace_id,
                    }
                    continue
                logger.exception("stream_agent failed")
                msg = str(exc).strip() or type(exc).__name__ or "unknown"
                trs.apply_terminal(thread_id, status="failed", error=msg, final_note=msg)
                yield {
                    "event": "error",
                    "node": "graph",
                    "data": {"message": msg},
                    "thread_id": thread_id,
                    "trace_id": trace_id,
                }
                ts = _task_status_event(thread_id, trace_id)
                if ts:
                    yield ts
                return


async def resume_with_approval(thread_id: str, decision: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
    """审批后从检查点恢复。"""
    graph = await get_compiled_graph()
    config = agent_run_config(thread_id)
    state = await graph.aget_state(config)
    repo = (state.values or {}).get("repo") or ""
    async for ev in stream_agent(
        message="",
        repo=repo,
        thread_id=thread_id,
        resume=True,
        approval_decision=decision,
    ):
        yield ev
