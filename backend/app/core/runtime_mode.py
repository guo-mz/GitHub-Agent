"""运行时持久化模式 —— 透明暴露 Redis / Checkpointer 能力，禁止虚假宣称可恢复性。"""

from __future__ import annotations

from typing import Any, Dict

from app.config import get_settings
from app.core.checkpointer import get_checkpointer_mode
from app.services.redis_service import redis_service


def runtime_persistence_status() -> Dict[str, Any]:
    from app.core.checkpointer import checkpointer_status, get_checkpointer_init_error

    ckpt = get_checkpointer_mode()
    ckpt_st = checkpointer_status()
    redis_ok = (not redis_service.fallback) and bool(getattr(redis_service, "_client", None))
    durable_checkpoint = ckpt == "redis"
    durable_idempotency = redis_ok
    settings = get_settings()
    hitl_allowed = durable_checkpoint or bool(settings.allow_ephemeral_hitl)
    init_err = get_checkpointer_init_error()
    return {
        "checkpointer": ckpt,
        "checkpointer_type": ckpt_st.get("type") or "",
        "checkpointer_init_error": init_err,
        "redis_fallback": bool(redis_service.fallback),
        "redis_connected": redis_ok,
        "durable_checkpoint": durable_checkpoint,
        "durable_idempotency": durable_idempotency,
        "hitl_resume_guaranteed": durable_checkpoint,
        "allow_ephemeral_hitl": bool(settings.allow_ephemeral_hitl),
        "new_hitl_tasks_allowed": hitl_allowed,
        "auth_mode": _auth_mode_label(),
        "note": (
            (
                f"MemorySaver 仅进程内有效，不能跨重启恢复 interrupt"
                + (f"；AsyncRedisSaver 失败原因: {init_err}" if init_err else "")
                + "；无 Redis 时发布幂等不可跨进程保证。"
            )
            if not durable_checkpoint
            else "AsyncRedisSaver 可用，interrupt 可跨进程恢复（需 Redis Stack）。"
        ),
    }


def _auth_mode_label() -> str:
    from app.core.security_auth import auth_mode

    return auth_mode()


def assert_hitl_task_allowed(*, action: str = "start_hitl") -> None:
    """启动依赖审批恢复的任务前调用；不满足则抛 HTTPException。"""
    from fastapi import HTTPException

    from app.services import audit_service

    st = runtime_persistence_status()
    if st.get("new_hitl_tasks_allowed"):
        return
    audit_service.record_security_event(
        action="persistence_gate",
        result="denied",
        reason="ephemeral_checkpoint",
        operation=action,
        extra={"checkpointer": st.get("checkpointer")},
    )
    raise HTTPException(
        status_code=503,
        detail=(
            "持久化 checkpoint 不可用（当前为 MemorySaver / 无 Redis Stack）。"
            "已拒绝启动依赖跨重启恢复的审批任务，避免虚假可恢复性。"
            "请启动 Redis Stack 并确保 AsyncRedisSaver 可用，"
            "或仅本地调试时设置 ALLOW_EPHEMERAL_HITL=true（不安全、不可跨进程恢复）。"
        ),
    )
