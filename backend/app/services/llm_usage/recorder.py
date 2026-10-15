"""记录每次真实模型尝试；包装 ainvoke / astream。"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from langchain_core.messages import BaseMessage

from app.services.llm_usage.context import get_llm_call_context, resolve_module_node
from app.services.llm_usage.extract import (
    UsageExtract,
    classify_error,
    extract_from_response,
    merge_chunk_usage,
)
from app.services.llm_usage.pricing import cost_to_float, estimate_cost
from app.services.llm_usage import store as usage_store

logger = logging.getLogger(__name__)


def _utc_iso(ts: Optional[float] = None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def build_record(
    *,
    provider: str,
    requested_model: str,
    call_type: str,
    status: str,
    started: float,
    finished: float,
    label: str = "",
    actual_model: Optional[str] = None,
    usage: Optional[UsageExtract] = None,
    error_type: Optional[str] = None,
    retry_count: int = 0,
    attempt_index: int = 0,
    call_group_id: str = "",
    time_to_first_token_ms: Optional[int] = None,
    record_id: str = "",
) -> Dict[str, Any]:
    ctx = get_llm_call_context()
    module_name, node_name = resolve_module_node(label=label or ctx.label)
    usage = usage or UsageExtract()
    model_for_price = actual_model or requested_model or ""
    cost = estimate_cost(
        provider=provider,
        model=model_for_price,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )
    latency_ms = max(0, int((finished - started) * 1000))
    return {
        "id": record_id or uuid.uuid4().hex,
        "call_group_id": call_group_id or uuid.uuid4().hex,
        "attempt_index": attempt_index,
        "trace_id": ctx.trace_id or None,
        "task_id": ctx.task_id or None,
        "conversation_id": ctx.thread_id or None,
        "module_name": module_name,
        "node_name": node_name,
        "provider": provider or None,
        "requested_model": requested_model or None,
        "actual_model": actual_model,  # 无元数据则为 None，禁止填 requested
        "call_type": call_type,
        "started_at": _utc_iso(started),
        "finished_at": _utc_iso(finished),
        "latency_ms": latency_ms,
        "time_to_first_token_ms": time_to_first_token_ms,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "estimated_cost": cost_to_float(cost.estimated_cost),
        "currency": cost.currency,
        "native_cost": cost_to_float(cost.native_cost),
        "native_currency": cost.native_currency,
        "fx_rate": cost_to_float(cost.fx_rate),
        "fx_as_of": cost.fx_as_of,
        "fx_source": cost.fx_source,
        "price_version": cost.price_version,
        "pricing_snapshot": cost.pricing_snapshot,
        "estimate_limitations": cost.estimate_limitations,
        "status": status,
        "error_type": error_type,
        "retry_count": retry_count,
        "usage_source": usage.usage_source,
        "created_at": _utc_iso(),
    }


async def record_llm_attempt(record: Dict[str, Any]) -> Dict[str, Any]:
    """持久化；失败不影响主链路。"""
    try:
        return await usage_store.insert_record(record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_llm_attempt failed: %s", exc)
        return record


async def ainvoke_tracked(
    llm: Any,
    messages: Sequence[BaseMessage],
    *,
    provider: str,
    requested_model: str,
    label: str = "llm",
    call_type: str = "chat",
    call_group_id: str = "",
    attempt_index: int = 0,
    retry_count: int = 0,
    timeout: Optional[float] = None,
) -> Any:
    """单次 ainvoke + 记录。timeout 由调用方用 wait_for 包一层亦可。"""
    import asyncio

    group = call_group_id or uuid.uuid4().hex
    started = time.perf_counter()
    t0 = time.time()
    try:
        if timeout is not None:
            resp = await asyncio.wait_for(llm.ainvoke(list(messages)), timeout=timeout)
        else:
            resp = await llm.ainvoke(list(messages))
        finished = time.perf_counter()
        usage = extract_from_response(resp, requested_model=requested_model)
        rec = build_record(
            provider=provider,
            requested_model=requested_model,
            call_type=call_type,
            status="ok",
            started=t0,
            finished=t0 + (finished - started),
            label=label,
            actual_model=usage.actual_model,
            usage=usage,
            retry_count=retry_count,
            attempt_index=attempt_index,
            call_group_id=group,
        )
        await record_llm_attempt(rec)
        return resp
    except BaseException as exc:
        finished = time.perf_counter()
        status, err = classify_error(exc)
        rec = build_record(
            provider=provider,
            requested_model=requested_model,
            call_type=call_type,
            status=status,
            started=t0,
            finished=t0 + (finished - started),
            label=label,
            actual_model=None,
            usage=UsageExtract(usage_source="unavailable"),
            error_type=err,
            retry_count=retry_count,
            attempt_index=attempt_index,
            call_group_id=group,
        )
        await record_llm_attempt(rec)
        raise


async def astream_tracked(
    llm: Any,
    messages: Sequence[BaseMessage],
    *,
    provider: str,
    requested_model: str,
    label: str = "llm",
    call_group_id: str = "",
    attempt_index: int = 0,
    retry_count: int = 0,
) -> AsyncIterator[Any]:
    """流式调用：记录 TTFT 与终态 usage（若服务商提供）。"""
    group = call_group_id or uuid.uuid4().hex
    started = time.perf_counter()
    t0 = time.time()
    ttft_ms: Optional[int] = None
    usage = UsageExtract()
    last_chunk = None
    try:
        async for chunk in llm.astream(list(messages)):
            if ttft_ms is None:
                content = getattr(chunk, "content", None)
                if content:
                    ttft_ms = max(0, int((time.perf_counter() - started) * 1000))
            usage = merge_chunk_usage(usage, chunk)
            last_chunk = chunk
            yield chunk
        finished = time.perf_counter()
        # 部分实现把 usage 放在最终聚合；再尝试从 last_chunk 读
        if usage.usage_source != "provider_metadata" and last_chunk is not None:
            usage = extract_from_response(last_chunk, requested_model=requested_model)
        rec = build_record(
            provider=provider,
            requested_model=requested_model,
            call_type="stream",
            status="ok",
            started=t0,
            finished=t0 + (finished - started),
            label=label,
            actual_model=usage.actual_model,
            usage=usage,
            retry_count=retry_count,
            attempt_index=attempt_index,
            call_group_id=group,
            time_to_first_token_ms=ttft_ms,
        )
        await record_llm_attempt(rec)
    except BaseException as exc:
        finished = time.perf_counter()
        status, err = classify_error(exc)
        rec = build_record(
            provider=provider,
            requested_model=requested_model,
            call_type="stream",
            status=status,
            started=t0,
            finished=t0 + (finished - started),
            label=label,
            actual_model=usage.actual_model,
            usage=usage if usage.usage_source == "provider_metadata" else UsageExtract(),
            error_type=err,
            retry_count=retry_count,
            attempt_index=attempt_index,
            call_group_id=group,
            time_to_first_token_ms=ttft_ms,
        )
        await record_llm_attempt(rec)
        raise


def primary_provider_info() -> tuple[str, str]:
    """旁路调用用：当前主配置 provider 名与 requested_model。"""
    from app.agents.llm_utils import _providers

    providers = _providers()
    if not providers:
        from app.config import get_settings

        s = get_settings()
        return "unknown", (s.llm_model or "")
    p = providers[0]
    return p.name, p.model
