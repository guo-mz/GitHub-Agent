"""幂等锁：同一业务操作（如 create_pr:repo:branch:title）只执行一次。

面试点：用 Redis SET key NX PX ttl —— 原子获取锁；执行失败可主动释放，
成功则让 TTL 自然过期，窗口内重复请求直接返回缓存结果。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional


def build_idempotency_key(operation: str, **parts: Any) -> str:
    """结构化幂等键，避免碰撞。"""
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"idem:{operation}:{digest}"


class IdempotencyResult:
    __slots__ = ("acquired", "cached_value")

    def __init__(self, acquired: bool, cached_value: Optional[str] = None):
        self.acquired = acquired
        self.cached_value = cached_value
