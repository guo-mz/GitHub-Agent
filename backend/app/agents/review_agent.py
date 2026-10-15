"""Review Agent（Phase 2 轻量）—— 只消费真实 Diff，不做自动测试 / 不发布。"""

from __future__ import annotations

from typing import Any, Dict, List

from app.agents.contracts import AgentEnvelope, utc_now


def run_review_on_diffs(
    *,
    task_id: str,
    repo: str,
    diffs: List[Dict[str, Any]],
    combined_diff: str,
) -> AgentEnvelope:
    started = utc_now()
    findings: List[str] = []
    if not diffs:
        findings.append("无真实 Diff 可供审查")
        status = "failed"
    else:
        status = "succeeded"
        total_add = sum(int(d.get("additions") or 0) for d in diffs)
        total_del = sum(int(d.get("deletions") or 0) for d in diffs)
        findings.append(f"共 {len(diffs)} 个文件，+{total_add}/-{total_del}")
        for d in diffs[:20]:
            findings.append(f"{d.get('status')}: {d.get('path')} (+{d.get('additions')}/-{d.get('deletions')})")
        if len(combined_diff) > 200_000:
            findings.append("警告：Diff 过大，请人工重点审查")

    return AgentEnvelope(
        task_id=task_id,
        agent_name="review",
        status=status,
        input_context={"repo": repo, "diff_files": len(diffs)},
        structured_output={"findings": findings, "based_on": "workspace_unified_diff"},
        artifacts={"combined_diff": combined_diff[:50000], "diffs": diffs},
        error=None if status == "succeeded" else "empty_diff",
        started_at=started,
        finished_at=utc_now(),
    )
