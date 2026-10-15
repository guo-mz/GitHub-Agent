"""LangGraph 各节点实现 —— 只返回增量 dict。

面试点：节点无副作用写全局 state；interrupt 用 langgraph.types.interrupt。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt

from app.agent.prompts import AGENT_SYSTEM, CLASSIFY_SYSTEM, FINALIZE_SYSTEM, PLAN_SYSTEM
from app.agent.state import AgentState
from app.config import get_settings

# 中国时区：给模型注入「今天」，避免幻觉日期
_TZ_SH = timezone(timedelta(hours=8))


def _now_str() -> str:
    return datetime.now(_TZ_SH).strftime("%Y-%m-%d %H:%M:%S %z")
from app.core.retry import with_retry
from app.services import audit_service
from app.services.guardrails import ALWAYS_APPROVE_TOOLS, HIGH_RISK_TOOLS, estimate_line_diff, run_guardrails
from app.services.intent import (
    OUT_OF_SCOPE_REPLY,
    classify_by_rules,
    detect_out_of_scope,
    extract_slots,
    format_intent_hint,
    has_active_dev_context,
    merge_llm,
)
from app.services.context_engine import compact_context, truncate_tool_content
from app.services.jev_service import decide_agent_turn, timeline_label as jev_timeline_label
from app.services.redis_service import redis_service
from app.agents.tool_sets import WRITE_TOOL_NAMES, is_write_tool, tools_for_agent
from app.agents.analysis_agent import run_analysis
from app.agents.planning_agent import run_planning
from app.agents.coding_agent import run_coding
from app.agents.review_agent import run_review_on_diffs
from app.agents.testing_agent import run_testing
from app.agents.publish_agent import run_publish
from app.agents.contracts import ApprovalKind, utc_now
from app.services import workspace_service as workspace_service
from app.tools import ALL_TOOLS

logger = logging.getLogger(__name__)


def _user_text(state: AgentState) -> str:
    messages = state.get("messages") or []
    for m in reversed(messages):
        if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human":
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def _append_envelope(state: AgentState, envelope: dict) -> list:
    prev = list(state.get("agent_envelopes") or [])
    prev.append(envelope)
    return prev[-40:]

# 待审批工具调用暂存于 state.approval_payload["pending_tool_calls"]


def _llm(temperature: float = 0.1, *, streaming: bool = True) -> ChatOpenAI:
    """主链路客户端（流式 thought 用）；非流式优先走 ainvoke_llm 多层兜底。"""
    from app.agents.llm_utils import make_chat_llm

    return make_chat_llm(temperature=temperature, streaming=streaming)


def _retry_settings():
    s = get_settings()
    return (
        max(1, int(s.agent_llm_max_retries)),
        max(1, int(s.agent_tool_max_retries)),
        float(s.agent_retry_base_delay),
    )


async def _llm_invoke(llm: Any, msgs: list, *, label: str = "llm"):
    """优先阿里云→DeepSeek→Ollama；失败再对传入 llm 做有限重试（每次尝试独立记用量）。"""
    from app.agents.llm_utils import ainvoke_llm
    from app.services.llm_usage.context import llm_call_context
    from app.services.llm_usage.recorder import ainvoke_tracked, primary_provider_info

    try:
        return await ainvoke_llm(msgs, temperature=0.1, label=label)
    except Exception as primary_exc:  # noqa: BLE001
        logger.warning("%s chain failed (%s), legacy retry on bound llm", label, primary_exc)

    attempts, _, base = _retry_settings()
    provider, model = primary_provider_info()
    # 若传入 llm 带 model_name，用作 requested_model
    req_model = getattr(llm, "model_name", None) or getattr(llm, "model", None) or model
    import uuid as _uuid

    call_group_id = _uuid.uuid4().hex
    attempt_box = {"n": 0}

    async def _once():
        idx = attempt_box["n"]
        attempt_box["n"] = idx + 1
        with llm_call_context(label=label):
            return await ainvoke_tracked(
                llm,
                msgs,
                provider=provider,
                requested_model=str(req_model),
                label=label,
                call_type="chat",
                call_group_id=call_group_id,
                attempt_index=idx,
                retry_count=idx,
            )

    def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        logger.info("%s retry %s after %s (sleep %.2fs)", label, attempt, exc, delay)

    return await with_retry(
        _once,
        attempts=attempts,
        base_delay=base,
        on_retry=_on_retry,
        label=label,
    )


async def _tool_invoke(fn: Any, args: dict, *, label: str):
    _, attempts, base = _retry_settings()
    return await with_retry(
        lambda: fn.ainvoke(args),
        attempts=attempts,
        base_delay=base,
        label=label,
    )


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if m:
            return json.loads(m.group(1))
        raise


async def classify_intent(state: AgentState) -> Dict[str, Any]:
    """意图识别：越界规则快路径 → 规则分类 → 必要时 LLM 校准。"""
    tid = state.get("trace_id") or ""
    await redis_service.track_trajectory(tid, "classify_intent")
    if await redis_service.is_circuit_open(tid):
        return {
            "error": "轨迹熔断：疑似死循环，已停止",
            "task_type": "qa",
            "final_answer": "执行中止：循环保护触发",
        }

    messages = state.get("messages") or []
    user_text = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human":
            user_text = m.content if isinstance(m.content, str) else str(m.content)
            break

    slots = extract_slots(user_text, default_repo=str(state.get("repo") or ""))

    # 明显职责外且无活跃开发上下文：不调 LLM，直接收尾（跳过 Jev/Agent/工具）
    oos = detect_out_of_scope(user_text)
    if oos and oos.out_of_scope and not has_active_dev_context(messages, state):
        oos.slots = {**slots, **(oos.slots or {})}
        return {
            "task_type": oos.task_type,
            "intent": oos.to_dict(),
            "final_answer": OUT_OF_SCOPE_REPLY,
            "messages": [AIMessage(content=f"[classify] {format_intent_hint(oos)}")],
        }

    rule = classify_by_rules(user_text)
    intent = rule

    # 规则不够确信时才问 LLM，避免慢且不可解释；失败则规则/qa 兜底
    if rule is None or rule.confidence < 0.8:
        try:
            llm = _llm(0)
            hint = ""
            if rule:
                hint = f"\n规则候选：{rule.task_type}（{rule.reason}）"
            msgs = [
                SystemMessage(content=CLASSIFY_SYSTEM),
                HumanMessage(content=(user_text or "") + hint),
            ]
            resp = await _llm_invoke(llm, msgs, label="classify_intent")
            data = _extract_json(resp.content)
            intent = merge_llm(rule, str(data.get("task_type") or "qa"), str(data.get("reason") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("classify_intent LLM fallback: %s", exc)
            intent = rule or merge_llm(None, "qa", f"LLM 失败: {exc}")

    intent.slots = {**slots, **(intent.slots or {})}
    payload = intent.to_dict()
    return {
        "task_type": intent.task_type,
        "intent": payload,
        "messages": [AIMessage(content=f"[classify] {format_intent_hint(intent)}")],
    }


async def jev_decide(state: AgentState) -> Dict[str, Any]:
    """Jev System One：有界决策（任务类型 / 风险 / 是否升 HITL / 是否规划）。

    LLM 负责生成；Jev 负责 typed 分支。无 TYPESAFE_API_KEY 时本地启发式兜底。
    """
    await redis_service.track_trajectory(state.get("trace_id") or "", "jev_decide")
    messages = state.get("messages") or []
    user_text = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human":
            user_text = m.content if isinstance(m.content, str) else str(m.content)
            break

    bundle = await decide_agent_turn(
        user_text=user_text,
        repo=str(state.get("repo") or ""),
        task_hint=str(state.get("task_type") or ""),
        intent=state.get("intent") or {},
    )
    payload = bundle.to_dict()
    out: Dict[str, Any] = {
        "jev": payload,
        "messages": [AIMessage(content=f"[jev] {jev_timeline_label(bundle)}")],
    }

    # 规则意图置信不高时，允许 Jev choice 校准 task_type
    intent = state.get("intent") or {}
    rule_conf = float(intent.get("confidence") or 0)
    jev_task = str(bundle.get("task_type") or "")
    jev_conf = bundle.confidence("task_type", 0.0)
    if jev_task in {"read", "write", "qa", "review"} and (
        rule_conf < 0.8 or not state.get("task_type")
    ):
        if jev_conf >= 0.45 or bundle.mode == "api":
            out["task_type"] = jev_task
            intent2 = {**intent, "task_type": jev_task, "jev_calibrated": True}
            out["intent"] = intent2

    return out


async def repo_analysis_node(state: AgentState) -> Dict[str, Any]:
    """Analysis Agent：只读仓库分析，写入 analysis_report。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "repo_analysis")
    tid = state.get("trace_id") or ""
    env = await run_analysis(
        task_id=tid,
        repo=str(state.get("repo") or ""),
        user_message=_user_text(state),
        ref="main",
    )
    report = env.structured_output or {}
    summary = (
        f"[analysis] stack={report.get('tech_stack')} "
        f"files={len(report.get('relevant_files') or [])} "
        f"uncertainties={len(report.get('uncertainties') or [])}"
    )
    return {
        "phase": "analyzing",
        "analysis_report": report,
        "agent_envelopes": _append_envelope(state, env.model_dump()),
        "messages": [AIMessage(content=summary)],
        "error": env.error or "",
    }


async def planning_agent_node(state: AgentState) -> Dict[str, Any]:
    """Planning Agent：结构化计划（非旧版简陋 step 列表）。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "planning_agent")
    tid = state.get("trace_id") or ""
    env = await run_planning(
        task_id=tid,
        repo=str(state.get("repo") or ""),
        user_message=_user_text(state),
        analysis=state.get("analysis_report") or {},
        task_type=str(state.get("task_type") or "write"),
    )
    plan_doc = env.structured_output or {}
    # 同步旧 plan 字段供冲突重试/提示兼容
    legacy = [
        {"step": s.get("step"), "action": s.get("action")}
        for s in (plan_doc.get("steps") or [])
        if isinstance(s, dict)
    ]
    title = plan_doc.get("title") or "修改计划"
    return {
        "phase": "planning",
        "plan_document": plan_doc,
        "plan": legacy,
        "plan_confirmed": False,
        "agent_envelopes": _append_envelope(state, env.model_dump()),
        "messages": [
            AIMessage(
                content=f"[plan_document] {title} files={plan_doc.get('files_to_change')}"
            )
        ],
        "error": env.error or "",
    }


async def await_plan_confirm_node(state: AgentState) -> Dict[str, Any]:
    """计划确认 HITL：approval_kind=plan_confirm，确认前禁止编码。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "await_plan_confirm")
    tid = state.get("trace_id") or "unknown"
    plan_doc = state.get("plan_document") or {}
    analysis = state.get("analysis_report") or {}
    payload = {
        "approval_kind": ApprovalKind.PLAN_CONFIRM.value,
        "tool_name": "plan_confirm",
        "operation": "plan_confirm",
        "risk_level": "medium",
        "repo": state.get("repo"),
        "plan_document": plan_doc,
        "analysis_report": {
            "tech_stack": analysis.get("tech_stack"),
            "relevant_files": analysis.get("relevant_files"),
            "uncertainties": analysis.get("uncertainties"),
        },
        "summary": plan_doc.get("title") or "请确认修改计划",
        "diff": json.dumps(plan_doc, ensure_ascii=False, indent=2)[:8000],
        "reasons": ["计划确认后才会进入编码阶段；拒绝则不会修改仓库"],
        "task_id": tid,
        "phase": "awaiting_plan_confirm",
    }
    pending_row = {
        "thread_id": tid,
        "task_id": tid,
        "approval_kind": ApprovalKind.PLAN_CONFIRM.value,
        "operation": "plan_confirm",
        "risk_level": "medium",
        "repo": state.get("repo"),
        "summary": payload["summary"],
        "diff": payload["diff"],
        "payload": payload,
        "status": "pending",
        "phase": "awaiting_plan_confirm",
    }
    await redis_service.save_pending_approval(tid, pending_row)
    audit_service.record_approval_pending(
        thread_id=tid,
        repo=str(state.get("repo") or ""),
        operation="plan_confirm",
        risk_level="medium",
        summary=str(payload["summary"]),
        diff=payload["diff"],
        payload=payload,
    )

    decision = interrupt(
        {
            "type": "plan_confirm_needed",
            "approval_kind": ApprovalKind.PLAN_CONFIRM.value,
            "payload": payload,
            "repo": state.get("repo"),
            "task_id": tid,
            "message": "请确认修改计划后才能进入编码阶段",
        }
    )
    if isinstance(decision, str):
        decision = {"decision": decision}
    dec = (decision or {}).get("decision", "reject")
    confirmed = dec == "approve"
    # 批准与拒绝都必须关闭 pending，防止旧审批被重复 resume
    await redis_service.resolve_approval(
        tid,
        decision=dec,
        approval_kind=ApprovalKind.PLAN_CONFIRM.value,
    )
    return {
        "phase": "awaiting_coding" if confirmed else "cancelled",
        "plan_confirmed": confirmed,
        "approval_kind": ApprovalKind.PLAN_CONFIRM.value,
        "approval_decision": dec,
        "approval_payload": payload,
        "need_approval": False,
        "messages": [
            AIMessage(
                content=f"[plan_confirm] decision={dec} comment={(decision or {}).get('comment', '')}"
            )
        ],
        "final_answer": (
            ""
            if confirmed
            else "用户已拒绝或取消修改计划，未进入编码阶段，仓库未被修改。"
        ),
    }


async def coding_agent_node(state: AgentState) -> Dict[str, Any]:
    """Phase 2：仅在隔离工作区编码，产出真实 Diff；不写远程 GitHub。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "coding_agent")
    tid = state.get("trace_id") or "unknown"
    if not state.get("plan_confirmed"):
        return {
            "phase": "cancelled",
            "error": "plan_not_confirmed",
            "coding_blocked_reason": "计划未确认，禁止编码",
            "final_answer": "计划未确认，已阻止编码，仓库与工作区均未修改。",
            "messages": [AIMessage(content="[coding_blocked] plan_not_confirmed")],
        }

    user_text = _user_text(state)
    env = await run_coding(
        task_id=tid,
        repo=str(state.get("repo") or ""),
        user_message=user_text,
        plan_document=state.get("plan_document") or {},
        analysis_report=state.get("analysis_report") or {},
        plan_confirmed=True,
    )
    envs = _append_envelope(state, env.model_dump())
    if env.status != "succeeded":
        testing = {
            "task_id": tid,
            "agent_name": "testing",
            "status": "not_run",
            "input_context": {"reason": "no_sandbox"},
            "structured_output": {},
            "artifacts": {},
            "error": None,
            "started_at": utc_now(),
            "finished_at": utc_now(),
        }
        publish = {**testing, "agent_name": "publish", "status": "not_run"}
        envs = list(envs) + [testing, publish]
        return {
            "phase": "coding_failed",
            "plan_confirmed": True,
            "workspace_id": tid,
            "workspace_diff": {},
            "error": env.error or "coding_failed",
            "agent_envelopes": envs[-40:],
            "final_answer": f"编码失败：{env.error or 'unknown'}（未修改远程仓库）",
            "messages": [AIMessage(content=f"[coding_failed] {env.error}")],
        }

    snap = {
        "task_id": tid,
        "repo": state.get("repo"),
        "ref": "main",
        "workspace_root": (env.artifacts or {}).get("workspace_root"),
        "files_touched": (env.artifacts or {}).get("files_touched") or [],
        "diffs": (env.artifacts or {}).get("diffs") or [],
        "combined_diff": (env.artifacts or {}).get("combined_diff") or "",
        "stats": (env.structured_output or {}).get("diff_stats") or {},
    }
    # 用磁盘再算一次，确保 Diff 来自真实文件而非模型口述
    try:
        snap = workspace_service.snapshot_to_dict(
            tid, repo=str(state.get("repo") or ""), ref="main"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("snapshot rebuild failed: %s", exc)

    review_env = run_review_on_diffs(
        task_id=tid,
        repo=str(state.get("repo") or ""),
        diffs=list(snap.get("diffs") or []),
        combined_diff=str(snap.get("combined_diff") or ""),
    )
    testing_stub = {
        "task_id": tid,
        "agent_name": "testing",
        "status": "not_run",
        "input_context": {"reason": "no_reliable_sandbox"},
        "structured_output": {"checks": []},
        "artifacts": {},
        "error": None,
        "started_at": utc_now(),
        "finished_at": utc_now(),
    }
    publish_stub = {**testing_stub, "agent_name": "publish", "status": "not_run"}
    envs = list(envs)
    envs.append(review_env.model_dump())
    envs.extend([testing_stub, publish_stub])

    files = ", ".join(snap.get("files_touched") or []) or "—"
    answer = (
        "## 工作区编码完成（未写远程）\n\n"
        f"- 变更文件：{files}\n"
        f"- Diff 统计：{json.dumps(snap.get('stats') or {}, ensure_ascii=False)}\n\n"
        "请审查真实 Diff 后再确认。Testing/Publish 保持 not_run。"
    )
    return {
        "phase": "awaiting_diff_approve",
        "plan_confirmed": True,
        "workspace_id": tid,
        "workspace_diff": snap,
        "diff_approved": False,
        "agent_envelopes": envs[-40:],
        "changed_files": list(snap.get("files_touched") or []),
        "final_answer": answer,
        "messages": [AIMessage(content=answer)],
        "error": "",
    }


async def await_diff_approve_node(state: AgentState) -> Dict[str, Any]:
    """Diff 审批 HITL：展示前先冻结快照，用户批准的是同一 content_hash。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "await_diff_approve")
    tid = state.get("trace_id") or "unknown"
    repo = str(state.get("repo") or "")
    ref = str((state.get("workspace_diff") or {}).get("ref") or "main")

    # 展示前冻结：Diff 必须由不可变快照计算，而非可变 work/
    try:
        manifest = workspace_service.freeze_approved_snapshot(tid)
        content_hash = str(manifest.get("content_hash") or "")
        if not content_hash:
            raise ValueError("empty_content_hash")
        snap = workspace_service.snapshot_from_approved(tid, repo=repo, ref=ref)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pre-display freeze failed")
        return {
            "phase": "coding_failed",
            "diff_approved": False,
            "approved_content_hash": "",
            "error": f"freeze_snapshot_failed:{exc}",
            "final_answer": f"Diff 展示前固化快照失败：{exc}（未发布）",
            "messages": [AIMessage(content=f"[diff_approve] freeze_failed {exc}")],
        }

    combined = str(snap.get("combined_diff") or "")
    payload = {
        "approval_kind": ApprovalKind.DIFF_APPROVE.value,
        "tool_name": "diff_approve",
        "operation": "diff_approve",
        "risk_level": "high",
        "repo": state.get("repo"),
        "summary": (
            f"冻结 Diff 待确认（{len(snap.get('diffs') or [])} 文件，"
            f"hash={content_hash[:16]}）"
        ),
        "diff": combined[:120000],
        "content_hash": content_hash,
        "workspace_diff": {
            "files_touched": snap.get("files_touched"),
            "diffs": snap.get("diffs"),
            "stats": snap.get("stats"),
            "workspace_root": snap.get("workspace_root"),
            "content_hash": content_hash,
            "source": "approved_snapshot",
        },
        "reasons": [
            "Diff 由冻结 approved_snapshot 计算，与待发布字节同源",
            "批准即确认该 content_hash；快照被改须重新审批",
            "确认 Diff ≠ 发布 PR；仍须测试门禁 + 发布审批",
        ],
        "task_id": tid,
        "phase": "awaiting_diff_approve",
    }
    pending_row = {
        "thread_id": tid,
        "task_id": tid,
        "approval_kind": ApprovalKind.DIFF_APPROVE.value,
        "operation": "diff_approve",
        "risk_level": "high",
        "repo": state.get("repo"),
        "summary": payload["summary"],
        "diff": payload["diff"],
        "content_hash": content_hash,
        "payload": payload,
        "status": "pending",
        "phase": "awaiting_diff_approve",
    }
    await redis_service.save_pending_approval(tid, pending_row)
    audit_service.record_approval_pending(
        thread_id=tid,
        repo=repo,
        operation="diff_approve",
        risk_level="high",
        summary=str(payload["summary"]),
        diff=payload["diff"][:8000],
        payload=payload,
    )
    decision = interrupt(
        {
            "type": "diff_approve_needed",
            "approval_kind": ApprovalKind.DIFF_APPROVE.value,
            "payload": payload,
            "repo": state.get("repo"),
            "task_id": tid,
            "content_hash": content_hash,
            "message": f"请确认冻结 Diff（hash={content_hash[:16]}，不会自动创建 PR）",
        }
    )
    if isinstance(decision, str):
        decision = {"decision": decision}
    dec = (decision or {}).get("decision", "reject")
    # 用户可附带确认同一版本；若显式给出不同 hash 则拒绝
    decision_hash = str((decision or {}).get("content_hash") or "").strip()
    approved = dec == "approve"
    await redis_service.resolve_approval(
        tid,
        decision=dec,
        approval_kind=ApprovalKind.DIFF_APPROVE.value,
    )
    if not approved:
        try:
            workspace_service.discard_workspace(tid)
            workspace_service.clear_approved_snapshot(tid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("discard workspace/snapshot failed: %s", exc)
        return {
            "phase": "cancelled",
            "diff_approved": False,
            "approved_content_hash": "",
            "workspace_diff": snap,
            "approval_kind": ApprovalKind.DIFF_APPROVE.value,
            "approval_decision": dec,
            "need_approval": False,
            "final_answer": "用户已拒绝 Diff，工作区与冻结快照已丢弃，远程仓库未修改。",
            "messages": [
                AIMessage(content=f"[diff_approve] decision={dec} discarded_workspace=1")
            ],
        }

    if decision_hash and decision_hash != content_hash:
        workspace_service.clear_approved_snapshot(tid)
        return {
            "phase": "coding_failed",
            "diff_approved": False,
            "approved_content_hash": "",
            "error": "diff_version_mismatch",
            "final_answer": (
                f"审批版本与展示冻结哈希不一致（shown={content_hash[:16]} "
                f"decision={decision_hash[:16]}），需重新审查 Diff。"
            ),
            "messages": [AIMessage(content="[diff_approve] version_mismatch")],
        }

    # 批准后不得静默重新冻结：只校验展示时的快照仍完整
    ok, reason, _loaded, _frozen = workspace_service.verify_and_load_approved_snapshot(
        tid, content_hash
    )
    if not ok:
        from app.services import audit_service as _audit

        _audit.record_security_event(
            action="content_hash",
            result="denied",
            reason=f"post_approve_snapshot:{reason}",
            task_id=tid,
            repo=repo,
            content_hash=content_hash[:16],
            approval_kind=ApprovalKind.DIFF_APPROVE.value,
        )
        try:
            await redis_service.resolve_approval(
                tid,
                decision=f"invalidated:{reason}",
                approval_kind=ApprovalKind.DIFF_APPROVE.value,
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "phase": "coding_failed",
            "diff_approved": False,
            "approved_content_hash": "",
            "error": f"snapshot_tampered:{reason}",
            "final_answer": (
                f"Diff 批准后快照已变化或损坏（{reason}），审批已失效，请重新编码并审批。"
            ),
            "messages": [AIMessage(content=f"[diff_approve] snapshot_tampered {reason}")],
        }

    from app.services import audit_service as _audit

    _audit.record_security_event(
        action="content_hash",
        result="allowed",
        reason="diff_approved_frozen_version",
        task_id=tid,
        repo=repo,
        content_hash=content_hash[:16],
        approval_kind=ApprovalKind.DIFF_APPROVE.value,
    )
    return {
        "phase": "testing",
        "diff_approved": True,
        "approved_content_hash": content_hash,
        "workspace_diff": snap,
        "approval_kind": ApprovalKind.DIFF_APPROVE.value,
        "approval_decision": dec,
        "need_approval": False,
        "final_answer": "",
        "messages": [
            AIMessage(
                content=f"[diff_approve] decision={dec} content_hash={content_hash[:16]} next=testing"
            )
        ],
    }


async def testing_agent_node(state: AgentState) -> Dict[str, Any]:
    """Testing Agent：无沙箱 → not_run；失败可触发有限次修复。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "testing_agent")
    tid = state.get("trace_id") or ""
    wd = state.get("workspace_diff") or {}
    prev_repair = int(state.get("repair_count") or 0)
    # 若从失败回环进入，先记一次修复
    report_prev = state.get("test_report") or {}
    repair = prev_repair + (1 if report_prev.get("failed") else 0)

    content_hash = str(state.get("approved_content_hash") or "")
    env = run_testing(
        task_id=tid,
        repo=str(state.get("repo") or ""),
        workspace_root=str(wd.get("workspace_root") or ""),
        requested_commands=[],
        content_hash=content_hash,
    )
    out = env.structured_output or {}
    report = {
        "status": env.status,
        "passed": bool(out.get("passed")),
        "failed": bool(out.get("failed")),
        "not_run": bool(out.get("not_run") or env.status == "not_run"),
        "unavailable": bool(out.get("unavailable") or env.status == "unavailable"),
        "checks": out.get("checks") or [],
        "summary": out.get("summary"),
        "repair_count": repair,
        "content_hash": content_hash or out.get("content_hash") or "",
    }
    from app.agent.edges import MAX_REPAIR

    envs = _append_envelope(state, env.model_dump())
    note = (
        f"Testing={env.status}; not_run={report['not_run']}; "
        f"failed={report['failed']}; repair={repair}"
    )
    go_publish = (not report["failed"]) or repair >= MAX_REPAIR
    return {
        "phase": "awaiting_publish" if go_publish else "repairing",
        "test_report": report,
        "repair_count": repair,
        "agent_envelopes": envs[-40:],
        "error": "" if go_publish or not report["failed"] else (env.error or ""),
        "messages": [AIMessage(content=f"[testing] {note}")],
        "final_answer": "",
    }


async def await_publish_approve_node(state: AgentState) -> Dict[str, Any]:
    """独立 publish 审批：通过前禁止 GitHub 写接口。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "await_publish_approve")
    tid = state.get("trace_id") or "unknown"
    report = state.get("test_report") or {}
    wd = state.get("workspace_diff") or {}
    from app.agents.publish_agent import evaluate_test_gate
    from app.config import get_settings

    allow_untrusted = bool(get_settings().allow_untrusted_publish)
    ok_test, test_reason = evaluate_test_gate(report, allow_untrusted=allow_untrusted)
    if not ok_test:
        from app.services import audit_service as _audit

        _audit.record_security_event(
            action="test_gate",
            result="denied",
            reason=test_reason,
            task_id=tid,
            repo=str(state.get("repo") or ""),
            content_hash=str(state.get("approved_content_hash") or "")[:16],
        )
        msg = (
            f"测试门禁阻止发布：{test_reason}。"
            "当前无安全隔离执行器时测试为 not_run/unavailable，不能进入发布审批。"
        )
        return {
            "phase": "publish_blocked",
            "publish_approved": False,
            "need_approval": False,
            "final_answer": msg,
            "messages": [AIMessage(content=f"[publish_blocked] {test_reason}")],
            "error": f"test_gate_blocked:{test_reason}",
        }
    if allow_untrusted and test_reason.startswith("local_untrusted"):
        from app.services import audit_service as _audit

        _audit.record_security_event(
            action="test_gate",
            result="allowed",
            reason=test_reason,
            task_id=tid,
            repo=str(state.get("repo") or ""),
            content_hash=str(state.get("approved_content_hash") or "")[:16],
        )
    content_hash = str(state.get("approved_content_hash") or "")
    payload = {
        "approval_kind": ApprovalKind.PUBLISH.value,
        "tool_name": "publish",
        "operation": "publish",
        "risk_level": "critical",
        "repo": state.get("repo"),
        "summary": "发布到 GitHub（创建分支/提交/PR）",
        "diff": str(wd.get("combined_diff") or "")[:120000],
        "content_hash": content_hash,
        "workspace_diff": {
            "files_touched": wd.get("files_touched"),
            "stats": wd.get("stats"),
            "content_hash": content_hash or wd.get("content_hash"),
            "source": wd.get("source") or "approved_snapshot",
        },
        "test_report": {
            "status": report.get("status"),
            "not_run": report.get("not_run"),
            "failed": report.get("failed"),
            "passed": report.get("passed"),
            "checks": report.get("checks") or [],
            "content_hash": report.get("content_hash") or content_hash,
        },
        "reasons": [
            "publish 与 plan_confirm / diff_approve 相互独立",
            "未批准前不得调用 GitHub 写接口",
            f"须绑定冻结 content_hash={content_hash[:16] or '—'}",
            (
                "Testing=not_run（无可靠沙箱），发布需人工确认风险"
                if report.get("not_run")
                else f"Testing status={report.get('status')}"
            ),
        ],
        "task_id": tid,
        "phase": "awaiting_publish",
    }
    pending_row = {
        "thread_id": tid,
        "task_id": tid,
        "approval_kind": ApprovalKind.PUBLISH.value,
        "operation": "publish",
        "risk_level": "critical",
        "repo": state.get("repo"),
        "summary": payload["summary"],
        "diff": payload["diff"],
        "payload": payload,
        "status": "pending",
        "phase": "awaiting_publish",
    }
    await redis_service.save_pending_approval(tid, pending_row)
    audit_service.record_approval_pending(
        thread_id=tid,
        repo=str(state.get("repo") or ""),
        operation="publish",
        risk_level="critical",
        summary=str(payload["summary"]),
        diff=payload["diff"][:8000],
        payload=payload,
    )
    decision = interrupt(
        {
            "type": "approval_needed",
            "approval_kind": ApprovalKind.PUBLISH.value,
            "payload": payload,
            "repo": state.get("repo"),
            "task_id": tid,
            "message": "请确认是否发布到 GitHub（创建分支/PR）",
        }
    )
    if isinstance(decision, str):
        decision = {"decision": decision}
    dec = (decision or {}).get("decision", "reject")
    approved = dec == "approve"
    await redis_service.resolve_approval(
        tid, decision=dec, approval_kind=ApprovalKind.PUBLISH.value
    )
    if not approved:
        return {
            "phase": "cancelled",
            "publish_approved": False,
            "approval_kind": ApprovalKind.PUBLISH.value,
            "approval_decision": dec,
            "need_approval": False,
            "final_answer": "用户已拒绝发布。工作区 Diff 保留，远程仓库未写入。",
            "messages": [AIMessage(content=f"[publish] decision={dec}")],
        }
    return {
        "phase": "publishing",
        "publish_approved": True,
        "approval_kind": ApprovalKind.PUBLISH.value,
        "approval_decision": dec,
        "need_approval": False,
        "final_answer": "",
        "messages": [AIMessage(content="[publish] decision=approve next=publish_agent")],
    }


async def publish_agent_node(state: AgentState) -> Dict[str, Any]:
    """仅在三门齐开 + 测试通过 + 哈希匹配后推送远程。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "publish_agent")
    tid = state.get("trace_id") or ""
    env = await run_publish(
        task_id=tid,
        repo=str(state.get("repo") or ""),
        plan_document=state.get("plan_document") or {},
        publish_approved=bool(state.get("publish_approved")),
        diff_approved=bool(state.get("diff_approved")),
        plan_confirmed=bool(state.get("plan_confirmed")),
        approved_content_hash=str(state.get("approved_content_hash") or ""),
        test_report=state.get("test_report") or {},
    )
    envs = _append_envelope(state, env.model_dump())
    art = env.artifacts or {}
    if env.status == "succeeded" and art.get("pr_url"):
        answer = (
            "## 已发布到 GitHub\n\n"
            f"- PR: {art.get('pr_url')}\n"
            f"- 分支: {art.get('branch')}\n"
            f"- commit: {art.get('commit_sha') or '—'}\n"
        )
        return {
            "phase": "published",
            "publish_result": env.structured_output or {},
            "agent_envelopes": envs[-40:],
            "final_answer": answer,
            "messages": [AIMessage(content=answer)],
            "error": "",
        }
    from app.agents.publish_agent import format_publish_error

    err_txt = format_publish_error(env.error or "publish_failed")
    answer = (
        f"## 发布失败\n\n{err_txt}\n\n"
        "工作区 Diff 仍保留；远程未写入。"
        "修好 Token 权限后可点「失败重试」。"
    )
    return {
        "phase": "publish_failed",
        "publish_result": env.structured_output or {},
        "agent_envelopes": envs[-40:],
        "error": err_txt,
        "final_answer": answer,
        "messages": [AIMessage(content=answer)],
    }


# 兼容旧图节点名（若检查点仍指向 coding_deferred）
async def coding_deferred_node(state: AgentState) -> Dict[str, Any]:
    return await coding_agent_node(state)


async def plan_node(state: AgentState) -> Dict[str, Any]:
    """旧版任务规划（冲突重试用）。主路径已改用 planning_agent_node。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "planner")
    user_text = _user_text(state)

    prompt = PLAN_SYSTEM.format(
        repo=state.get("repo") or "",
        task_type=state.get("task_type") or "qa",
        intent_slots=json.dumps((state.get("intent") or {}).get("slots") or {}, ensure_ascii=False),
        user_message=user_text,
    )
    plan: List[Any] = []
    try:
        llm = _llm(0.2)
        resp = await _llm_invoke(llm, [SystemMessage(content=prompt)], label="planner")
        plan = _extract_json(resp.content)
        if not isinstance(plan, list):
            plan = [{"step": 1, "action": str(plan)}]
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan fallback: %s", exc)
        if state.get("task_type") == "write":
            plan = [
                {"step": 1, "action": "必要时 review / get_last_review / suggest_fix"},
                {"step": 2, "action": "创建特性分支"},
                {"step": 3, "action": "update_file 写入修复（带 SHA）"},
                {"step": 4, "action": "创建 PR（需审批）"},
            ]
        elif state.get("task_type") == "review":
            plan = [
                {"step": 1, "action": "定位待审查文件或 PR"},
                {"step": 2, "action": "review_file / review_pr（含静态扫描）"},
                {"step": 3, "action": "呈现风险等级与优先修复清单"},
                {"step": 4, "action": "可选 create_issue 登记问题"},
            ]
        else:
            plan = [{"step": 1, "action": "检索并阅读相关代码/Issue"}, {"step": 2, "action": "总结回答"}]

    retry = int(state.get("retry_count") or 0)
    bump = 1 if state.get("conflict") else 0
    return {
        "plan": plan,
        "conflict": False,
        "retry_count": retry + bump,
        "messages": [AIMessage(content=f"[plan] {json.dumps(plan, ensure_ascii=False)}")],
    }


async def compact_context_node(state: AgentState) -> Dict[str, Any]:
    """上下文工程节点：按 token 预算裁剪 + 可选摘要，写入 context_summary。

    不删 checkpointer 里的完整 messages，只准备「工作上下文」元数据供 agent 使用。
    """
    await redis_service.track_trajectory(state.get("trace_id") or "", "compact_context")
    settings = get_settings()
    llm = None
    if settings.context_llm_summary:
        try:
            llm = _llm(0.1, streaming=False)
        except Exception:  # noqa: BLE001
            llm = None
    pack = await compact_context(
        state.get("messages") or [],
        existing_summary=str(state.get("context_summary") or ""),
        max_tokens=int(settings.context_max_tokens),
        keep_recent=int(settings.context_keep_recent),
        tool_max_chars=int(settings.context_tool_max_chars),
        enable_llm_summary=bool(settings.context_llm_summary),
        llm=llm,
    )
    return {
        "context_summary": pack.summary,
        "context_meta": pack.meta.as_dict(),
    }


def _working_messages(state: AgentState) -> list:
    """用已压缩的 summary + 最近窗口组进 LLM 的消息（同步、不调 LLM）。"""
    from app.services.context_engine import build_context_pack

    settings = get_settings()
    pack = build_context_pack(
        state.get("messages") or [],
        existing_summary=str(state.get("context_summary") or ""),
        max_tokens=int(settings.context_max_tokens),
        keep_recent=int(settings.context_keep_recent),
        tool_max_chars=int(settings.context_tool_max_chars),
        force_summary=str(state.get("context_summary") or "") or None,
    )
    return pack.messages


async def agent_node(state: AgentState) -> Dict[str, Any]:
    """ReAct 思考 + 决策（可能发出 tool_calls）。

    兜底：带工具调用重试 → 去掉工具再答一次 → 最后返回可读错误。
    消息经上下文工程窗口后再进模型，避免多轮工具输出撑爆 context。
    """
    await redis_service.track_trajectory(state.get("trace_id") or "", "agent")
    if await redis_service.is_circuit_open(state.get("trace_id") or ""):
        return {
            "error": "轨迹熔断",
            "messages": [AIMessage(content="检测到可能的死循环，已停止继续调用工具。")],
        }

    system = AGENT_SYSTEM.format(
        repo=state.get("repo") or "",
        task_type=state.get("task_type") or "qa",
        intent_slots=json.dumps((state.get("intent") or {}).get("slots") or {}, ensure_ascii=False),
        plan=json.dumps(state.get("plan_document") or state.get("plan") or [], ensure_ascii=False),
        now=_now_str(),
    )
    if not state.get("plan_confirmed"):
        system += (
            "\n\n【权限】计划尚未确认：禁止调用 create_branch/update_file/create_pr/"
            "merge_pr/suggest_fix 等写操作工具。"
        )
    working = _working_messages(state)
    msgs = [SystemMessage(content=system), *working]
    allowed = tools_for_agent(
        plan_confirmed=bool(state.get("plan_confirmed")),
        allow_publish=False,
    )
    llm_tools = _llm(0.2).bind_tools(allowed)

    try:
        resp = await _llm_invoke(llm_tools, msgs, label="agent")
        return {"messages": [resp]}
    except Exception as primary:  # noqa: BLE001
        logger.warning("agent with-tools exhausted, degrade without tools: %s", primary)

    # 降级：无工具纯文本回答（面试可讲：工具链挂了仍能给结论）
    try:
        plain = _llm(0.2, streaming=True)
        degrade_msgs = [
            SystemMessage(
                content=system
                + "\n\n注意：工具暂时不可用，请仅基于已有上下文直接给出中文结论，不要调用工具。"
            ),
            *working,
        ]
        resp = await _llm_invoke(plain, degrade_msgs, label="agent_degrade")
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        note = f"（已降级：工具调用失败后改为纯文本回答）\n\n{text}".strip()
        return {
            "messages": [AIMessage(content=note)],
            "final_answer": note,
            "error": f"degraded: {primary}",
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent LLM failed after degrade")
        answer = (
            f"模型连续调用失败，已停止重试。\n"
            f"原因：{exc}\n"
            f"建议：稍后点「新会话」再试，或检查 LLM API Key / 网络。"
        )
        return {
            "error": str(exc),
            "messages": [AIMessage(content=answer)],
            "final_answer": answer,
        }


async def tools_node(state: AgentState) -> Dict[str, Any]:
    """执行工具；高危工具先标记 need_approval，真正执行在 tools_approved。

    计划未确认时硬拦截写工具（即使模型绕过 bind_tools 列表）。
    """
    await redis_service.track_trajectory(state.get("trace_id") or "", "tools")
    messages = state.get("messages") or []
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return {}

    plan_ok = bool(state.get("plan_confirmed"))
    # 未确认计划：写/发布工具全部硬拦，绝不进入发布审批
    if not plan_ok:
        blocked_msgs: List[ToolMessage] = []
        blocked_meta: List[dict] = []
        for tc in tool_calls:
            name = tc.get("name")
            if is_write_tool(str(name)) or name in ALWAYS_APPROVE_TOOLS:
                call_id = tc.get("id") or name
                msg = {
                    "ok": False,
                    "error": "计划未确认，禁止调用编码/发布工具",
                    "tool": name,
                    "blocked_by": "plan_confirm",
                }
                blocked_msgs.append(
                    ToolMessage(
                        content=json.dumps(msg, ensure_ascii=False),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                blocked_meta.append({"name": name, "blocked": True, "reasons": [msg["error"]]})
        # 仍允许只读工具继续执行
        tool_calls = [
            tc
            for tc in tool_calls
            if not is_write_tool(str(tc.get("name"))) and tc.get("name") not in ALWAYS_APPROVE_TOOLS
        ]
        if not tool_calls:
            return {
                "messages": blocked_msgs,
                "last_tool_results": blocked_meta,
                "coding_blocked_reason": "计划未确认",
            }
        # 把拦截结果先记下，后面与只读结果合并
        results_prefix = blocked_msgs
        last_prefix = blocked_meta
    else:
        results_prefix = []
        last_prefix = []

    # 安全边界：任何远程写工具均不得在 ReAct tools 节点直接执行（含 update_file/create_branch）。
    # 仅 Publish Agent 在持有 write_gate 许可时可写 GitHub。
    remote_write_names = set(ALWAYS_APPROVE_TOOLS) | {
        "create_branch",
        "update_file",
        "create_issue",
        "add_comment",
        "suggest_fix",
    }
    blocked_writes = [tc for tc in tool_calls if tc.get("name") in remote_write_names]
    executable = [tc for tc in tool_calls if tc.get("name") not in remote_write_names]
    pending: list = []  # 不再通过 ReAct 挂起执行写工具

    tool_map = {t.name: t for t in ALL_TOOLS}
    results: List[ToolMessage] = list(results_prefix)
    changed: List[str] = []
    conflict = False
    last_results: List[dict] = list(last_prefix)

    for tc in blocked_writes:
        name = tc.get("name")
        call_id = tc.get("id") or name
        msg = {
            "ok": False,
            "error": (
                "远程写工具已禁用旁路：create_branch/update_file/create_pr/merge_pr 等"
                "仅能由 Publish Agent 在计划+Diff+测试+发布审批通过后执行。"
            ),
            "tool": name,
            "blocked_by": "write_gate",
        }
        results.append(
            ToolMessage(
                content=json.dumps(msg, ensure_ascii=False),
                tool_call_id=call_id,
                name=name,
            )
        )
        last_results.append({"name": name, "blocked": True, "reasons": [msg["error"]]})

    for tc in executable:
        name = tc.get("name")
        args = tc.get("args") or {}
        call_id = tc.get("id") or name
        # 强制使用会话仓库：模型常把仓库名 "-" 误写成 owner/owner
        if state.get("repo"):
            args = {**args, "repo": state["repo"]}

        fn = tool_map.get(name)
        if not fn:
            results.append(ToolMessage(content=f"未知工具: {name}", tool_call_id=call_id, name=name))
            continue
        try:
            raw = await _tool_invoke(fn, args, label=f"tool:{name}")
        except Exception as exc:  # noqa: BLE001
            raw = json.dumps(
                {"ok": False, "error": str(exc), "retried": True},
                ensure_ascii=False,
            )
        # 工具观察写入前截断，防止单次巨大文件内容撑爆后续上下文
        tool_body, _ = truncate_tool_content(
            str(raw), int(get_settings().context_tool_max_chars)
        )
        results.append(ToolMessage(content=tool_body, tool_call_id=call_id, name=name))
        last_results.append({"name": name, "args": args, "result": tool_body[:2000]})

        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("conflict"):
            conflict = True
        if name == "update_file" and isinstance(parsed, dict) and parsed.get("ok"):
            changed.append(args.get("path") or "")

    out: Dict[str, Any] = {
        "messages": results,
        "last_tool_results": last_results,
        "conflict": conflict,
    }
    if changed:
        out["changed_files"] = changed

    if pending:
        from app.services.approval_format import format_pending_diff

        changed_all = list(state.get("changed_files") or []) + changed
        reasons = ["高危操作必须人工审批：批准后才会调用 GitHub 写接口"]
        jev = state.get("jev") or {}
        esc_ans = (jev.get("answers") or {}).get("escalate_hitl") or {}
        try:
            esc = float(esc_ans.get("value") if isinstance(esc_ans, dict) else 0)
        except (TypeError, ValueError):
            esc = 0.0
        if esc >= float(get_settings().jev_escalate_threshold):
            reasons.append(f"Jev escalate_hitl={esc:.2f}（System One 建议升级 HITL）")
        out["need_approval"] = True
        out["approval_kind"] = ApprovalKind.PUBLISH.value
        out["approval_payload"] = {
            "approval_kind": ApprovalKind.PUBLISH.value,
            "pending_tool_calls": pending,
            "tool_name": pending[0].get("name"),
            "tool_args": pending[0].get("args") or {},
            "diff": format_pending_diff(pending, changed_all, repo=str(state.get("repo") or "")),
            "reasons": reasons,
            "risk_level": "critical" if pending[0].get("name") == "merge_pr" else "high",
            "repo": state.get("repo"),
            "changed_files": changed_all,
            "jev": jev,
            "task_id": state.get("trace_id"),
            "phase": state.get("phase") or "awaiting_publish",
        }
        # 为 pending 工具先占位，避免 OpenAI 要求每个 tool_call 都有 response
        for tc in pending:
            results.append(
                ToolMessage(
                    content=json.dumps({"ok": False, "pending_approval": True}, ensure_ascii=False),
                    tool_call_id=tc.get("id") or tc.get("name"),
                    name=tc.get("name"),
                )
            )
        out["messages"] = results

    return out


async def guardrail_check(state: AgentState) -> Dict[str, Any]:
    """六层护栏校验（对最近工具结果做二次审查）。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "guardrail_check")

    if state.get("need_approval") and state.get("approval_payload"):
        # 已有高危挂起
        payload = state["approval_payload"]
        tid = state.get("trace_id") or "unknown"
        pending_row = {
            "thread_id": tid,
            "operation": payload.get("tool_name"),
            "risk_level": payload.get("risk_level", "high"),
            "repo": state.get("repo"),
            "summary": "; ".join(payload.get("reasons") or []),
            "diff": payload.get("diff") or "",
            "payload": payload,
            "status": "pending",
        }
        await redis_service.save_pending_approval(tid, pending_row)
        audit_service.record_approval_pending(
            thread_id=tid,
            repo=str(state.get("repo") or ""),
            operation=str(payload.get("tool_name") or ""),
            risk_level=str(payload.get("risk_level") or "high"),
            summary=pending_row["summary"],
            diff=pending_row["diff"],
            payload=payload,
        )
        return {"need_approval": True}

    # 审查最近 update_file
    for item in state.get("last_tool_results") or []:
        if item.get("name") != "update_file":
            continue
        args = item.get("args") or {}
        gr = run_guardrails(
            "update_file",
            args,
            changed_files=list(state.get("changed_files") or []),
            lines_changed=estimate_line_diff(args.get("content") or ""),
            diff=f"path={args.get('path')}",
        )
        if gr.blocked:
            return {
                "error": "护栏拦截: " + "; ".join(gr.reasons),
                "need_approval": False,
                "messages": [AIMessage(content="护栏拦截: " + "; ".join(gr.reasons))],
            }
        if gr.need_approval:
            payload = gr.approval_payload
            tid = state.get("trace_id") or "unknown"
            pending_row = {
                "thread_id": tid,
                "operation": "update_file",
                "risk_level": gr.risk_level,
                "repo": state.get("repo"),
                "summary": "; ".join(gr.reasons),
                "diff": payload.get("diff") or "",
                "payload": payload,
                "status": "pending",
            }
            await redis_service.save_pending_approval(tid, pending_row)
            audit_service.record_approval_pending(
                thread_id=tid,
                repo=str(state.get("repo") or ""),
                operation="update_file",
                risk_level=str(gr.risk_level or "high"),
                summary=pending_row["summary"],
                diff=pending_row["diff"],
                payload=payload,
            )
            return {"need_approval": True, "approval_payload": payload}

    return {"need_approval": False}


async def request_approval(state: AgentState) -> Dict[str, Any]:
    """interrupt 审批 —— 仅用于发布类（publish），与 plan_confirm 分离。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "request_approval")
    payload = dict(state.get("approval_payload") or {})
    payload.setdefault("approval_kind", ApprovalKind.PUBLISH.value)
    tid = state.get("trace_id") or "unknown"
    pending_row = {
        "thread_id": tid,
        "task_id": tid,
        "approval_kind": ApprovalKind.PUBLISH.value,
        "operation": payload.get("tool_name") or "publish",
        "risk_level": payload.get("risk_level", "high"),
        "repo": state.get("repo"),
        "summary": "; ".join(payload.get("reasons") or []) or "发布审批",
        "diff": payload.get("diff") or "",
        "payload": payload,
        "status": "pending",
        "phase": "awaiting_publish",
    }
    await redis_service.save_pending_approval(tid, pending_row)
    decision = interrupt(
        {
            "type": "approval_needed",
            "approval_kind": ApprovalKind.PUBLISH.value,
            "payload": payload,
            "repo": state.get("repo"),
            "task_id": tid,
            "message": "发布操作等待人工审批（与计划确认分开）",
        }
    )
    # decision 期望 {"decision": "approve"|"reject", "comment": "..."}
    if isinstance(decision, str):
        decision = {"decision": decision}
    dec = (decision or {}).get("decision", "reject")
    return {
        "approval_decision": dec,
        "messages": [
            AIMessage(content=f"[approval] decision={dec} comment={(decision or {}).get('comment', '')}")
        ],
    }


async def tools_approved_node(state: AgentState) -> Dict[str, Any]:
    """遗留 HITL 写工具路径：明确拒绝，不得执行 create_pr/update_file 等远程写。

    旧 checkpoint 若恢复到本节点，也只能得到拒绝结果，不能旁路 Publish Agent。
    """
    await redis_service.track_trajectory(state.get("trace_id") or "", "tools_approved")
    payload = state.get("approval_payload") or {}
    pending = payload.get("pending_tool_calls") or []
    if not pending and payload.get("tool_name"):
        pending = [
            {
                "name": payload.get("tool_name"),
                "args": payload.get("tool_args") or {},
                "id": "approved_1",
            }
        ]

    from app.agents.tool_sets import WRITE_TOOL_NAMES, is_write_tool

    remote_write = set(WRITE_TOOL_NAMES) | {
        "create_pr",
        "merge_pr",
        "create_branch",
        "update_file",
        "create_or_update_file",
        "push",
        "publish",
    }
    last_results: List[dict] = []
    summaries: List[str] = []

    for tc in pending:
        name = str(tc.get("name") or "")
        if is_write_tool(name) or name in remote_write:
            msg = {
                "ok": False,
                "error": (
                    "遗留 tools_approved 写路径已关闭："
                    f"{name} 不得在此执行。远程写入仅允许 Publish Agent "
                    "在计划+Diff+测试+发布审批通过且持有 write_gate 时进行。"
                ),
                "tool": name,
                "blocked_by": "legacy_write_path_disabled",
            }
            raw = json.dumps(msg, ensure_ascii=False)
            last_results.append(
                {"name": name, "blocked": True, "result": raw, "reasons": [msg["error"]]}
            )
            summaries.append(f"{name} => REJECTED:{msg['error']}")
            audit_service.record_security_event(
                action="legacy_write_tool",
                result="denied",
                reason="tools_approved_disabled",
                operation=name,
                task_id=str(state.get("trace_id") or ""),
                repo=str(state.get("repo") or ""),
            )
            continue
        # 非写工具：本节点也不再执行（只读应走 ReAct tools）
        msg = {
            "ok": False,
            "error": f"tools_approved 仅用于拒绝遗留写工具；不执行 {name}",
            "tool": name,
            "blocked_by": "legacy_path_disabled",
        }
        raw = json.dumps(msg, ensure_ascii=False)
        last_results.append({"name": name, "blocked": True, "result": raw})
        summaries.append(f"{name} => REJECTED")

    await redis_service.resolve_approval(state.get("trace_id") or "")
    return {
        "messages": [
            AIMessage(content="[approved_execution_rejected]\n" + "\n".join(summaries))
        ],
        "last_tool_results": last_results,
        "need_approval": False,
        "approval_payload": {},
        "approval_decision": "",
        "error": "legacy_write_path_disabled",
    }


async def reject_node(state: AgentState) -> Dict[str, Any]:
    """拒绝/护栏拦截 —— 无副作用收尾。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "reject")
    await redis_service.resolve_approval(state.get("trace_id") or "")
    reason = state.get("error") or "用户拒绝或护栏拦截，操作已取消，无副作用。"
    # 若有 pending 高危工具，不执行即可（幂等锁未写入成功结果）
    return {
        "need_approval": False,
        "approval_payload": {},
        "final_answer": reason,
        "messages": [AIMessage(content=f"[rejected] {reason}")],
    }


async def finalize(state: AgentState) -> Dict[str, Any]:
    """收尾总结。"""
    await redis_service.track_trajectory(state.get("trace_id") or "", "finalize")
    if state.get("final_answer"):
        return {}

    # 若 agent 已给出可读回复，跳过二次 LLM 总结（提速）
    for m in reversed(state.get("messages") or []):
        content = getattr(m, "content", "")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text or text.startswith("["):
            continue
        if text.startswith("{") and "task_type" in text:
            continue
        if len(text) >= 20 and not getattr(m, "tool_calls", None):
            return {"final_answer": text, "messages": [AIMessage(content=text)]}

    try:
        llm = _llm(0.2)
        working = _working_messages(state)
        # finalize 再收紧一点，避免二次总结吃太多 token
        tail = working[-16:] if len(working) > 16 else working
        resp = await _llm_invoke(
            llm,
            [SystemMessage(content=FINALIZE_SYSTEM), *tail],
            label="finalize",
        )
        answer = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as exc:  # noqa: BLE001
        # 兜底：拼接最近消息，避免整轮空白
        parts = []
        for m in (state.get("messages") or [])[-8:]:
            content = getattr(m, "content", "")
            if content:
                parts.append(str(content)[:500])
        answer = "\n".join(parts) or f"完成（总结失败，已跳过 LLM：{exc}）"

    return {
        "final_answer": answer,
        "messages": [AIMessage(content=answer)],
    }
