"""LLM 调用上下文（contextvars）—— 避免改业务函数签名。"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterator, Optional

from app.services.llm_usage.modules import resolve_module_node as _resolve


@dataclass(frozen=True)
class LlmCallContext:
    trace_id: str = ""
    task_id: str = ""
    thread_id: str = ""  # conversation_id
    module_name: str = ""
    node_name: str = ""
    label: str = ""


_ctx: ContextVar[LlmCallContext] = ContextVar(
    "llm_call_context",
    default=LlmCallContext(),
)


def get_llm_call_context() -> LlmCallContext:
    return _ctx.get()


def resolve_module_node(
    *,
    label: str = "",
    module_name: str = "",
    node_name: str = "",
) -> tuple[str, str]:
    ctx = get_llm_call_context()
    return _resolve(
        label=label or ctx.label,
        module_name=module_name or ctx.module_name,
        node_name=node_name or ctx.node_name,
    )


@contextmanager
def llm_call_context(
    *,
    trace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    module_name: Optional[str] = None,
    node_name: Optional[str] = None,
    label: Optional[str] = None,
) -> Iterator[LlmCallContext]:
    cur = get_llm_call_context()
    nxt = replace(
        cur,
        trace_id=trace_id if trace_id is not None else cur.trace_id,
        task_id=task_id if task_id is not None else cur.task_id,
        thread_id=thread_id if thread_id is not None else cur.thread_id,
        module_name=module_name if module_name is not None else cur.module_name,
        node_name=node_name if node_name is not None else cur.node_name,
        label=label if label is not None else cur.label,
    )
    token: Token = _ctx.set(nxt)
    try:
        yield nxt
    finally:
        _ctx.reset(token)


def context_as_dict(ctx: Optional[LlmCallContext] = None) -> Dict[str, Any]:
    c = ctx or get_llm_call_context()
    return {
        "trace_id": c.trace_id or None,
        "task_id": c.task_id or None,
        "conversation_id": c.thread_id or None,
        "module_name": c.module_name or None,
        "node_name": c.node_name or None,
        "label": c.label or None,
    }
