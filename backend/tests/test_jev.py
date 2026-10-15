"""Jev 本地兜底 + 路由单测。"""

import asyncio

from app.agent.edges import route_after_classify, route_after_jev
from app.services.jev_service import _local_decide, decide_agent_turn, timeline_label


def test_local_review_escalates_plan():
    b = _local_decide("审查 README.md，给出风险等级", "review")
    assert b.get("task_type") == "review"
    assert float(b.get("need_planner") or 0) >= 0.5
    assert "Jev" in timeline_label(b)


def test_local_write_high_risk():
    b = _local_decide("修复后提 PR 并 merge 到 main", "write")
    assert b.get("task_type") == "write"
    assert float(b.get("escalate_hitl") or 0) >= 0.7
    assert float(b.get("risk") or 0) >= 1.5


def test_route_after_classify_goes_jev():
    assert route_after_classify({"task_type": "qa"}) == "jev_decide"


def test_route_after_classify_oos_finalize():
    assert (
        route_after_classify(
            {
                "intent": {"out_of_scope": True},
                "final_answer": "职责外提示",
            }
        )
        == "finalize"
    )


def test_route_after_jev_analysis_for_review_and_plan():
    """Phase 1：write/review（及 need_planner）进入 repo_analysis，而非旧 planner。"""
    assert route_after_jev({"task_type": "review", "jev": {}}) == "repo_analysis"
    assert route_after_jev({"task_type": "write", "jev": {}}) == "repo_analysis"
    assert (
        route_after_jev(
            {
                "task_type": "qa",
                "jev": {"answers": {"need_planner": {"value": 0.9}}},
            }
        )
        == "repo_analysis"
    )
    assert (
        route_after_jev(
            {
                "task_type": "qa",
                "jev": {"answers": {"need_planner": {"value": 0.1}}},
            }
        )
        == "compact_context"
    )


def test_decide_without_api_key_uses_local():
    async def _run():
        return await decide_agent_turn(user_text="解释一下这个项目", repo="guo-mz/-")

    b = asyncio.run(_run())
    assert b.mode in {"local", "disabled-local", "api"}
    assert "task_type" in b.answers
