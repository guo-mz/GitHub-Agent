"""内部 RPC 凭据校验 —— fail-closed，禁止可预测默认 Token。"""

from __future__ import annotations

import hmac
from typing import Optional

from app.config import get_settings

# 历史/示例弱凭据：即便写入环境变量也不得作为有效生产凭据
INSECURE_INTERNAL_RPC_TOKENS = frozenset(
    {
        "dev-internal-rpc",
        "changeme",
        "secret",
        "internal",
    }
)
MIN_INTERNAL_RPC_TOKEN_LEN = 16


def resolve_internal_rpc_token() -> str:
    """返回可用于鉴权的 Token；未配置/过短/弱默认时返回空串。"""
    raw = (get_settings().internal_rpc_token or "").strip()
    if not raw:
        return ""
    if raw in INSECURE_INTERNAL_RPC_TOKENS:
        return ""
    if len(raw) < MIN_INTERNAL_RPC_TOKEN_LEN:
        return ""
    return raw


def internal_rpc_token_configured() -> bool:
    return bool(resolve_internal_rpc_token())


def tokens_match(provided: Optional[str], expected: str) -> bool:
    """常量时间比较；长度不等或类型异常时返回 False（不抛）。"""
    a = (provided or "").strip()
    b = (expected or "").strip()
    if not a or not b:
        return False
    try:
        return hmac.compare_digest(a, b)
    except (TypeError, ValueError):
        return False
