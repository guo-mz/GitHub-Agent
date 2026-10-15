"""从 LangChain / OpenAI 兼容响应中提取 usage 与实际模型名。

不得用配置名冒充 actual_model；不得用 0 填充未知 Token。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class UsageExtract:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_source: str = "unavailable"  # provider_metadata | unavailable
    actual_model: Optional[str] = None
    raw_keys: Optional[Dict[str, Any]] = None


def _as_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _from_usage_dict(usage: Dict[str, Any]) -> Tuple[
    Optional[int], Optional[int], Optional[int], Optional[int], Optional[int]
]:
    inp = _as_int(
        usage.get("input_tokens")
        if usage.get("input_tokens") is not None
        else usage.get("prompt_tokens")
    )
    out = _as_int(
        usage.get("output_tokens")
        if usage.get("output_tokens") is not None
        else usage.get("completion_tokens")
    )
    total = _as_int(usage.get("total_tokens"))
    cached = None
    reasoning = None

    # OpenAI / 兼容：prompt_tokens_details.cached_tokens
    details = usage.get("prompt_tokens_details") or usage.get("input_token_details")
    if isinstance(details, dict):
        cached = _as_int(details.get("cached_tokens") or details.get("cache_read"))
    if cached is None:
        cached = _as_int(
            usage.get("cached_input_tokens")
            or usage.get("cache_read_input_tokens")
            or usage.get("cached_tokens")
        )

    out_details = usage.get("completion_tokens_details") or usage.get("output_token_details")
    if isinstance(out_details, dict):
        reasoning = _as_int(out_details.get("reasoning_tokens"))
    if reasoning is None:
        reasoning = _as_int(usage.get("reasoning_tokens"))

    input_details = usage.get("input_token_details")
    if isinstance(input_details, dict) and cached is None:
        cached = _as_int(input_details.get("cache_read") or input_details.get("cached_tokens"))

    if total is None and inp is not None and out is not None:
        total = inp + out
    return inp, out, cached, reasoning, total


def extract_from_response(resp: Any, *, requested_model: str = "") -> UsageExtract:
    """优先 usage_metadata，其次 response_metadata.token_usage。"""
    out = UsageExtract()
    if resp is None:
        return out

    meta = getattr(resp, "response_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    # actual model：仅来自响应元数据
    for key in ("model_name", "model", "model_id"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            out.actual_model = val.strip()
            break
    # 部分 SDK 放在 additional_kwargs
    if not out.actual_model:
        add = getattr(resp, "additional_kwargs", None) or {}
        if isinstance(add, dict):
            for key in ("model", "model_name"):
                val = add.get(key)
                if isinstance(val, str) and val.strip():
                    out.actual_model = val.strip()
                    break

    usage_meta = getattr(resp, "usage_metadata", None)
    usage_dict: Optional[Dict[str, Any]] = None
    if isinstance(usage_meta, dict) and usage_meta:
        usage_dict = usage_meta
    elif hasattr(usage_meta, "get"):
        try:
            usage_dict = dict(usage_meta)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            usage_dict = None
    elif usage_meta is not None and hasattr(usage_meta, "input_tokens"):
        usage_dict = {
            "input_tokens": getattr(usage_meta, "input_tokens", None),
            "output_tokens": getattr(usage_meta, "output_tokens", None),
            "total_tokens": getattr(usage_meta, "total_tokens", None),
            "input_token_details": getattr(usage_meta, "input_token_details", None),
            "output_token_details": getattr(usage_meta, "output_token_details", None),
        }

    if usage_dict is None:
        token_usage = meta.get("token_usage") or meta.get("usage")
        if isinstance(token_usage, dict) and token_usage:
            usage_dict = token_usage

    if not usage_dict:
        out.usage_source = "unavailable"
        return out

    inp, oup, cached, reasoning, total = _from_usage_dict(usage_dict)
    # 只要有任一 token 字段即视为 provider_metadata
    if all(v is None for v in (inp, oup, cached, reasoning, total)):
        out.usage_source = "unavailable"
        return out

    out.input_tokens = inp
    out.output_tokens = oup
    out.cached_input_tokens = cached
    out.reasoning_tokens = reasoning
    out.total_tokens = total
    out.usage_source = "provider_metadata"
    out.raw_keys = {k: usage_dict.get(k) for k in list(usage_dict.keys())[:12]}
    return out


def merge_chunk_usage(acc: UsageExtract, chunk: Any) -> UsageExtract:
    """流式：累积最后带 usage 的 chunk。"""
    part = extract_from_response(chunk)
    if part.actual_model:
        acc.actual_model = part.actual_model
    if part.usage_source == "provider_metadata":
        acc.input_tokens = part.input_tokens
        acc.output_tokens = part.output_tokens
        acc.cached_input_tokens = part.cached_input_tokens
        acc.reasoning_tokens = part.reasoning_tokens
        acc.total_tokens = part.total_tokens
        acc.usage_source = "provider_metadata"
        acc.raw_keys = part.raw_keys
    return acc


def classify_error(exc: BaseException) -> Tuple[str, str]:
    """返回 (status, error_type)。"""
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", "CancelledError"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout", "TimeoutError"
    name = type(exc).__name__
    msg = str(exc).lower()
    if "cancel" in name.lower() or "cancelled" in msg:
        return "cancelled", name
    if "timeout" in name.lower() or "timed out" in msg:
        return "timeout", name
    return "error", name
