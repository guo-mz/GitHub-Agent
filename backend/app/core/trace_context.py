"""跨服务追踪：X-Trace-Id / X-Span-Id 透传（W3C 简化版，面试可讲）。"""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

TRACE_HEADER = "x-trace-id"
SPAN_HEADER = "x-span-id"
PARENT_SPAN_HEADER = "x-parent-span-id"

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_span_id: ContextVar[str] = ContextVar("span_id", default="-")


def get_trace_id() -> str:
    return _trace_id.get()


def get_span_id() -> str:
    return _span_id.get()


def new_ids(incoming_trace: Optional[str] = None) -> tuple[str, str]:
    tid = (incoming_trace or "").strip() or uuid.uuid4().hex
    sid = uuid.uuid4().hex[:16]
    return tid, sid


class TraceMiddleware(BaseHTTPMiddleware):
    """注入/透传 Trace，写入 response header，并挂到 logging record。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        tid, sid = new_ids(request.headers.get(TRACE_HEADER))
        parent = request.headers.get(SPAN_HEADER) or request.headers.get(PARENT_SPAN_HEADER) or ""
        token_t = _trace_id.set(tid)
        token_s = _span_id.set(sid)
        # 让现有 TraceFilter 能读到
        request.state.trace_id = tid
        request.state.span_id = sid
        try:
            response = await call_next(request)
            response.headers[TRACE_HEADER] = tid
            response.headers[SPAN_HEADER] = sid
            if parent:
                response.headers[PARENT_SPAN_HEADER] = parent
            return response
        finally:
            _trace_id.reset(token_t)
            _span_id.reset(token_s)


class TraceLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        record.span_id = get_span_id() or "-"
        return True


def outbound_trace_headers() -> dict:
    """Gateway / Agent RPC 出站时带上。"""
    tid = get_trace_id()
    sid = get_span_id()
    h = {TRACE_HEADER: tid or uuid.uuid4().hex}
    if sid and sid != "-":
        h[PARENT_SPAN_HEADER] = sid
        h[SPAN_HEADER] = uuid.uuid4().hex[:16]
    return h
