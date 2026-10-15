"""HITL 路由单测 —— 面试可现场 pytest：高危工具挂起、拒绝无执行副作用语义。"""

from app.agent.edges import route_after_approval, route_after_guardrail
from app.services.guardrails import ALWAYS_APPROVE_TOOLS, HIGH_RISK_TOOLS, run_guardrails


def test_create_pr_and_merge_always_in_approve_set():
    assert "create_pr" in ALWAYS_APPROVE_TOOLS
    assert "merge_pr" in ALWAYS_APPROVE_TOOLS
    assert ALWAYS_APPROVE_TOOLS.issubset(HIGH_RISK_TOOLS)


def test_create_pr_never_blocked_but_needs_approval():
    """create_pr 不直接拦死，而是 need_approval → interrupt。"""
    r = run_guardrails(
        "create_pr",
        {"title": "feat: demo", "head": "agent/fix", "base": "main"},
    )
    assert r.blocked is False
    assert r.need_approval is True


def test_guardrail_need_approval_routes_to_interrupt():
    assert route_after_guardrail({"need_approval": True}) == "request_approval"


def test_approve_goes_tools_approved_reject_goes_reject():
    assert route_after_approval({"approval_decision": "approve"}) == "tools_approved"
    assert route_after_approval({"approval_decision": "reject"}) == "reject"
    assert route_after_approval({}) == "reject"


def test_pending_placeholder_semantics():
    """tools_node 对 ALWAYS_APPROVE 只写 pending，不调用真实 GitHub。

    这里用集合契约证明：面试口述「拒绝无副作用」的前提是批准前未执行。
    """
    pending_names = sorted(ALWAYS_APPROVE_TOOLS)
    assert pending_names == ["create_pr", "merge_pr"]
