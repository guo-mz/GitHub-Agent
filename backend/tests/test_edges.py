"""条件边单测 —— 证明状态机路由可预期。"""

from app.agent.edges import (
    route_after_agent,
    route_after_classify,
    route_after_guardrail,
    route_after_jev,
    route_after_plan_confirm,
    route_after_tools,
)


class _Msg:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls or []


def test_classify_goes_jev():
    assert route_after_classify({"task_type": "write"}) == "jev_decide"
    assert route_after_classify({"task_type": "qa"}) == "jev_decide"


def test_classify_oos_goes_finalize():
    assert (
        route_after_classify(
            {
                "task_type": "qa",
                "intent": {"out_of_scope": True},
                "final_answer": "我是专注于软件开发的 Coding Agent",
            }
        )
        == "finalize"
    )
    # 无 final_answer 不短路，避免误终止
    assert (
        route_after_classify({"intent": {"out_of_scope": True}, "final_answer": ""})
        == "jev_decide"
    )


def test_jev_routes_analysis_or_compact():
    assert route_after_jev({"task_type": "write"}) == "repo_analysis"
    assert route_after_jev({"task_type": "review"}) == "repo_analysis"
    assert route_after_jev({"task_type": "qa", "jev": {}}) == "compact_context"


def test_plan_confirm_routes():
    assert (
        route_after_plan_confirm({"approval_decision": "approve", "plan_confirmed": True})
        == "coding_agent"
    )
    assert route_after_plan_confirm({"approval_decision": "reject"}) == "finalize"


def test_coding_and_diff_routes():
    from app.agent.edges import route_after_coding, route_after_diff_approve

    assert (
        route_after_coding(
            {
                "workspace_diff": {
                    "combined_diff": "diff --git a/x b/x\n",
                    "diffs": [{"path": "x"}],
                }
            }
        )
        == "await_diff_approve"
    )
    assert route_after_coding({"error": "boom"}) == "finalize"
    assert route_after_coding({"workspace_diff": {}}) == "finalize"
    assert route_after_diff_approve({"diff_approved": True}) == "testing_agent"
    assert route_after_diff_approve({"diff_approved": False}) == "finalize"


def test_testing_and_publish_routes():
    from app.agent.edges import (
        MAX_REPAIR,
        route_after_publish_approve,
        route_after_testing,
    )

    assert (
        route_after_testing({"test_report": {"failed": True}, "repair_count": 0})
        == "coding_agent"
    )
    assert (
        route_after_testing(
            {"test_report": {"failed": True}, "repair_count": MAX_REPAIR}
        )
        == "await_publish_approve"
    )
    assert (
        route_after_testing({"test_report": {"not_run": True, "failed": False}})
        == "await_publish_approve"
    )
    assert route_after_publish_approve({"publish_approved": True}) == "publish_agent"
    assert route_after_publish_approve({"publish_approved": False}) == "finalize"


def test_agent_with_tools():
    assert route_after_agent({"messages": [_Msg([{"name": "x"}])]}) == "tools"
    assert route_after_agent({"messages": [_Msg()]}) == "finalize"


def test_guardrail_routes():
    assert route_after_guardrail({"need_approval": True}) == "request_approval"
    assert (
        route_after_guardrail({"error": "护栏拦截: main", "need_approval": False})
        == "reject"
    )
    assert route_after_guardrail({}) == "compact_context"


def test_tools_conflict_retries_planner():
    assert route_after_tools({"conflict": True, "retry_count": 0}) == "planner"
    assert route_after_tools({"conflict": True, "retry_count": 3}) == "compact_context"
    assert route_after_tools({"conflict": False}) == "guardrail_check"
