"""Langfuse 全链路追踪：节点路径、token、耗时、工具调用。

面试点：trace_id 贯穿 SSE / Agent / 工具层，面试时能画清 observability 链路。
无密钥时降级为 no-op，本地开发不阻塞。
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from typing import Any, Dict, Generator, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

_langfuse = None


def get_langfuse():
    global _langfuse
    if _langfuse is not None:
        return _langfuse
    settings = get_settings()
    pk = (settings.langfuse_public_key or "").strip()
    sk = (settings.langfuse_secret_key or "").strip()
    # 占位符会连假地址并拖慢/卡死 SSE，直接禁用
    if (
        not pk
        or not sk
        or "xxxx" in pk.lower()
        or "xxxx" in sk.lower()
        or pk.startswith("pk-lf-xxx")
    ):
        _langfuse = False
        return None
    try:
        from langfuse import Langfuse

        _langfuse = Langfuse(
            public_key=pk,
            secret_key=sk,
            host=settings.langfuse_host,
        )
        return _langfuse
    except Exception as exc:  # noqa: BLE001
        logger.warning("Langfuse init failed, tracing disabled: %s", exc)
        _langfuse = False
        return None


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextlib.contextmanager
def agent_trace(
    name: str,
    trace_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """上下文管理器：记录一次 Agent 执行。"""
    tid = trace_id or new_trace_id()
    ctx: Dict[str, Any] = {"trace_id": tid, "spans": [], "start": time.time()}
    lf = get_langfuse()
    trace = None
    if lf:
        try:
            trace = lf.trace(id=tid, name=name, metadata=metadata or {})
            ctx["trace"] = trace
        except Exception as exc:  # noqa: BLE001
            logger.warning("langfuse.trace failed: %s", exc)

    try:
        yield ctx
    finally:
        duration_ms = int((time.time() - ctx["start"]) * 1000)
        ctx["duration_ms"] = duration_ms
        if trace is not None:
            try:
                trace.update(metadata={"duration_ms": duration_ms, **(metadata or {})})
                lf.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning("langfuse flush failed: %s", exc)


def span_event(
    ctx: Dict[str, Any],
    name: str,
    input_data: Any = None,
    output_data: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """记录节点/工具 span。"""
    entry = {
        "name": name,
        "input": input_data,
        "output": output_data,
        "metadata": metadata or {},
        "ts": time.time(),
    }
    ctx.setdefault("spans", []).append(entry)
    trace = ctx.get("trace")
    if trace is not None:
        try:
            trace.span(name=name, input=input_data, output=output_data, metadata=metadata or {})
        except Exception as exc:  # noqa: BLE001
            logger.debug("span_event skipped: %s", exc)
