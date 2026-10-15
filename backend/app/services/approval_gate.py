"""审批 resume 门禁 —— 缺失 / 已拒绝 / 已过期 / 与任务状态不匹配时默认拒绝。

无 Redis 时依赖 approval_store 磁盘真相，防止旧 LangGraph checkpoint 被盲恢复。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from app.services import approval_store, task_run_service as trs


# resume 时任务应处于 HITL 等待；其它状态默认拒绝
_RESUME_OK_STATUS = frozenset({"awaiting_approval"})

# 这些任务状态一律禁止再 resume（含软终态）
_BLOCK_RESUME_STATUS = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "awaiting_coding",
        "diff_approved",
    }
)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def validate_approval_resume(
    tid: str,
    *,
    decision: str,
    approval_kind: Optional[str] = None,
    task_id: Optional[str] = None,
    pending: Optional[Dict[str, Any]] = None,
    resolved: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    max_pending_age_sec: int = 3600,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """返回 (ok, reason, pending_or_none)。默认拒绝。"""
    decision = (decision or "").lower()
    pending = pending or approval_store.get_pending(tid)
    resolved = resolved if resolved is not None else approval_store.get_resolved(tid)
    run = trs.get_run_by_thread(tid) or trs.get_run(tid)
    run_status = (run or {}).get("status") or ""

    # 1) 已 resolved：一律拒绝复用（含已拒绝）
    if resolved and not pending:
        dec = str(resolved.get("decision") or "").lower()
        return (
            False,
            f"approval already resolved: decision={dec} kind={resolved.get('approval_kind') or '-'}",
            None,
        )

    # 2) 无 pending：默认拒绝（不再允许「盲 resume checkpoint」）
    if not pending:
        # 任务已终态/软终态时给出更明确原因
        if run_status in _BLOCK_RESUME_STATUS:
            return False, f"no pending approval; task status={run_status} blocks resume", None
        return False, "no pending approval; resume denied by default", None

    # 3) pending 已标记非 pending
    st = str(pending.get("status") or "pending").lower()
    if st in {"rejected", "resolved", "expired", "cancelled"}:
        return False, f"pending approval status={st} cannot be resumed", pending

    # 4) 过期
    now = now or datetime.now(timezone.utc)
    created = _parse_ts(pending.get("updated_at") or pending.get("created_at"))
    if created is not None:
        age = (now - created.astimezone(timezone.utc)).total_seconds()
        if age > max_pending_age_sec:
            return False, f"pending approval expired (age={int(age)}s > {max_pending_age_sec}s)", pending

    # 5) kind 校验
    pending_kind = (
        pending.get("approval_kind")
        or (pending.get("payload") or {}).get("approval_kind")
        or "publish"
    )
    req_kind = approval_kind or pending_kind
    if approval_kind and approval_kind != pending_kind:
        return (
            False,
            f"approval_kind mismatch: pending={pending_kind} requested={approval_kind}",
            pending,
        )

    # 6) task_id 校验
    pending_task = pending.get("task_id") or pending.get("thread_id") or tid
    if task_id and task_id not in {pending_task, tid}:
        return False, f"task_id mismatch: pending={pending_task} requested={task_id}", pending

    # 7) 与任务状态匹配：无任务记录时仍允许（仅有 pending 的边缘情况），有记录则必须 awaiting_approval
    if run_status:
        if run_status in _BLOCK_RESUME_STATUS:
            return False, f"task status={run_status} incompatible with approval resume", pending
        if run_status not in _RESUME_OK_STATUS:
            return (
                False,
                f"task status={run_status} incompatible with approval_kind={pending_kind}",
                pending,
            )

    # 8) 对已拒绝决策：允许走 reject resume（关闭图），但不允许 approve
    # （调用方仍可传 reject；此处仅额外拦「对已 resolved-reject 的 approve」已在步骤 1 处理）

    if decision not in {"approve", "reject"}:
        return False, f"invalid decision={decision}", pending

    return True, "ok", pending
