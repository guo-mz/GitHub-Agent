"""Agent 重试兜底：瞬时故障可重试，永久错误立刻失败。

面试点：
- 只重试幂等/瞬时类（超时、429、5xx、连接断开）
- 指数退避 + 抖动，避免打爆上游
- 可注入 on_retry，方便 SSE 推「第 N 次重试」
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# HTTP / 上游常见瞬时状态码
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "temporarily",
    "rate limit",
    "ratelimit",
    "too many requests",
    "overloaded",
    "unavailable",
    "connection reset",
    "connection aborted",
    "connection error",
    "connect error",
    "server disconnected",
    "remote protocol",
    "502",
    "503",
    "504",
    "429",
    "econnreset",
    "broken pipe",
)


def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试（非认证/参数类永久错误）。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True

    code = _status_code(exc)
    if code is not None:
        if code in _RETRYABLE_STATUS:
            return True
        # 4xx 除上述外视为永久失败
        if 400 <= code < 500:
            return False

    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "ratelimit", "overload", "unavailable", "connection")):
        return True

    msg = str(exc).lower()
    return any(m in msg for m in _RETRYABLE_MARKERS)


async def with_retry(
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.6,
    max_delay: float = 8.0,
    retry_if: Callable[[BaseException], bool] = is_retryable,
    on_retry: Optional[Callable[[int, BaseException, float], Any]] = None,
    label: str = "op",
) -> T:
    """执行 factory()，瞬时失败则指数退避重试。

    factory 必须每次返回新的 awaitable（不能复用已 await 过的 coroutine）。
    """
    tries = max(1, int(attempts))
    last: Optional[BaseException] = None

    for attempt in range(1, tries + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= tries or not retry_if(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= 0.75 + random.random() * 0.5  # 抖动
            logger.warning(
                "%s failed attempt %s/%s (%s); retry in %.2fs",
                label,
                attempt,
                tries,
                exc,
                delay,
            )
            if on_retry:
                maybe = on_retry(attempt, exc, delay)
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[misc]
            await asyncio.sleep(delay)

    assert last is not None
    raise last
