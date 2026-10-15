"""LangGraph Checkpointer —— interrupt 后状态持久化，审批后从检查点恢复。

面试点：
- MemorySaver / InMemorySaver：仅进程内有效，不能跨重启恢复
- Redis Stack + AsyncRedisSaver：可跨进程/跨重启恢复（需 RediSearch）
- thread_id 是会话主键；recursion_limit 防死循环

禁止把 memory 模式描述成 Redis 持久化成功。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

_checkpointer: Any = None
_checkpointer_mode: str = "none"  # redis | memory | none
_checkpointer_init_error: str = ""
_checkpointer_type_name: str = ""


def get_checkpointer_mode() -> str:
    return _checkpointer_mode


def get_checkpointer_init_error() -> str:
    return _checkpointer_init_error


def reset_checkpointer() -> None:
    """测试/探测用：清除单例，下次 get_checkpointer 重新初始化。"""
    global _checkpointer, _checkpointer_mode, _checkpointer_init_error, _checkpointer_type_name
    _checkpointer = None
    _checkpointer_mode = "none"
    _checkpointer_init_error = ""
    _checkpointer_type_name = ""


async def get_checkpointer():
    """优先官方 AsyncRedisSaver（需 Redis Stack / RediSearch）→ MemorySaver。

    Redis 初始化失败时记录原因并降级；调用方应通过 get_checkpointer_mode()
    / runtime_persistence_status() 判断，不得假定已是 Redis 持久化。
    """
    global _checkpointer, _checkpointer_mode, _checkpointer_init_error, _checkpointer_type_name
    if _checkpointer is not None:
        return _checkpointer

    settings = get_settings()
    redis_url = (settings.redis_url or "").strip()

    # 1) 异步官方包（astream 必须 aget_tuple；同步 RedisSaver 会 NotImplementedError）
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver  # type: ignore

        saver = AsyncRedisSaver(redis_url=redis_url)
        await saver.asetup()
        _checkpointer = saver
        _checkpointer_mode = "redis"
        _checkpointer_init_error = ""
        _checkpointer_type_name = f"{type(saver).__module__}.{type(saver).__name__}"
        logger.info("Using official AsyncRedisSaver url=%s", redis_url)
        return _checkpointer
    except Exception as exc:  # noqa: BLE001
        _checkpointer_init_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "AsyncRedisSaver unavailable (%s); falling back to MemorySaver. "
            "HITL cross-restart resume is NOT available in memory mode.",
            _checkpointer_init_error,
        )

    # 2) 降级 MemorySaver（进程内）；审批/会话/幂等仍走 RedisService（可能另有连接）
    from langgraph.checkpoint.memory import MemorySaver

    _checkpointer = MemorySaver()
    _checkpointer_mode = "memory"
    _checkpointer_type_name = f"{type(_checkpointer).__module__}.{type(_checkpointer).__name__}"
    logger.warning(
        "Using MemorySaver; use Redis Stack (FT.*) + langgraph-checkpoint-redis "
        "for durable interrupt checkpoints. Approval payloads remain in RedisService."
    )
    return _checkpointer


def checkpointer_status() -> Dict[str, Any]:
    """只读快照：供 /health 与测试断言，禁止误报。"""
    durable = _checkpointer_mode == "redis"
    return {
        "mode": _checkpointer_mode,
        "type": _checkpointer_type_name,
        "durable": durable,
        "init_error": _checkpointer_init_error,
        "note": (
            "AsyncRedisSaver：interrupt 可跨进程恢复（需 Redis Stack）"
            if durable
            else "MemorySaver/InMemorySaver：仅进程内有效，不能跨重启恢复 interrupt"
        ),
    }


async def probe_checkpointer(*, force_reset: bool = False) -> Dict[str, Any]:
    """主动探测并返回状态（可选重置单例）。"""
    if force_reset:
        reset_checkpointer()
    await get_checkpointer()
    return checkpointer_status()


def agent_run_config(thread_id: str, recursion_limit: Optional[int] = None) -> dict:
    settings = get_settings()
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit or settings.agent_recursion_limit,
    }
