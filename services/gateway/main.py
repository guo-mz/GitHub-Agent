"""BFF Gateway：服务发现 + 熔断 + 跨服务 Trace + SSE 透传。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
for p in (str(_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.config import get_settings  # noqa: E402
from app.core.circuit_breaker import gateway_breaker  # noqa: E402
from app.core import service_registry  # noqa: E402
from app.core.trace_context import (  # noqa: E402
    TRACE_HEADER,
    TraceMiddleware,
    outbound_trace_headers,
)
from app.models.schemas import HealthResponse  # noqa: E402
from services.gateway.routing import resolve_upstream  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [gateway] trace=%(trace_id)s %(message)s",
)

settings = get_settings()
# 熔断参数可配
gateway_breaker.failure_threshold = int(settings.breaker_failure_threshold)
gateway_breaker.recovery_timeout = float(settings.breaker_recovery_sec)

app = FastAPI(title="GitHub Agent Gateway", version="1.0.0")
app.add_middleware(TraceMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-trace-id", "x-span-id", "x-parent-span-id"],
)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
    "content-encoding",
}


def _is_sse_path(path: str) -> bool:
    return path.startswith("/api/chat/") or path.endswith("/stream") or "/ai-edit/stream" in path


async def _upstream_base(name: str) -> tuple[str, str]:
    """返回 (url, source) source=discovery|static。"""
    discovered = await service_registry.resolve(name)
    if discovered:
        return discovered, "discovery"
    if name == "agent":
        return settings.agent_svc_url.rstrip("/"), "static"
    return settings.repo_svc_url.rstrip("/"), "static"


async def _probe(url: str) -> tuple[bool, dict]:
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(
                f"{url.rstrip('/')}/health",
                headers=outbound_trace_headers(),
            )
            if r.status_code >= 400:
                return False, {}
            ct = r.headers.get("content-type", "")
            return True, r.json() if ct.startswith("application/json") else {}
    except Exception:  # noqa: BLE001
        return False, {}


@app.get("/health", response_model=HealthResponse)
async def health():
    agent_url, agent_src = await _upstream_base("agent")
    repo_url, repo_src = await _upstream_base("repo")
    agent_ok, agent_h = await _probe(agent_url)
    repo_ok, repo_h = await _probe(repo_url)
    ok = agent_ok and repo_ok
    registry = await service_registry.list_services()
    agent_meta = ((registry.get("agent") or {}).get("meta") or {})
    rpc_on = bool(agent_meta.get("rpc") or agent_h.get("services", {}).get("repo_rpc"))
    return HealthResponse(
        status="ok" if ok else "degraded",
        redis=bool(agent_h.get("redis") or repo_h.get("redis")),
        postgres=bool(repo_h.get("postgres")),
        rag=bool(repo_h.get("rag")),
        rag_mode=str(repo_h.get("rag_mode") or "none"),
        checkpointer=str(agent_h.get("checkpointer") or "none"),
        # rerank 挂在 Repo（RAG）侧；Agent health 也可能带
        rerank=str(repo_h.get("rerank") or agent_h.get("rerank") or "keyword"),
        mode="micro",
        services={
            "agent": agent_ok,
            "repo": repo_ok,
            "gateway": True,
            "rpc": rpc_on,
        },
        discovery={
            "agent": {"url": agent_url, "source": agent_src, "alive": agent_ok},
            "repo": {"url": repo_url, "source": repo_src, "alive": repo_ok},
            "registry": registry,
        },
        breakers=gateway_breaker.snapshot(),
    )


@app.get("/internal/discovery")
async def discovery_debug():
    return {
        "services": await service_registry.list_services(),
        "breakers": gateway_breaker.snapshot(),
        "static": {
            "agent": settings.agent_svc_url,
            "repo": settings.repo_svc_url,
        },
    }


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_all(full_path: str, request: Request):
    path = request.url.path or ("/" + full_path if full_path else "/")
    target = resolve_upstream(path)
    if target is None:
        return JSONResponse({"detail": f"no upstream for {path}"}, status_code=404)

    if not gateway_breaker.allow(target):
        return JSONResponse(
            {
                "detail": f"circuit open for {target}",
                "breakers": gateway_breaker.snapshot().get(target),
            },
            status_code=503,
        )

    base, src = await _upstream_base(target)
    url = f"{base}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    # 强制带上当前 span 的出站追踪头
    headers.update(outbound_trace_headers())
    # 若客户端已有 trace，outbound 会用 context 里的（中间件已设）
    if TRACE_HEADER not in {k.lower(): k for k in headers}:
        pass
    body = await request.body()

    client = httpx.AsyncClient(timeout=None)
    try:
        upstream_req = client.build_request(
            request.method,
            url,
            headers=headers,
            content=body if body else None,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        gateway_breaker.record_failure(target)
        logger.exception("proxy failed → %s (%s)", target, src)
        return JSONResponse(
            {"detail": f"upstream {target} unreachable: {exc}", "source": src},
            status_code=502,
        )

    if upstream_resp.status_code >= 500:
        gateway_breaker.record_failure(target)
    else:
        gateway_breaker.record_success(target)

    media = upstream_resp.headers.get("content-type", "application/octet-stream")
    out_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in {"content-type", "content-encoding"}
    }
    out_headers["x-upstream"] = target
    out_headers["x-upstream-source"] = src
    origin = request.headers.get("origin")
    if origin:
        out_headers["access-control-allow-origin"] = origin
        out_headers["access-control-allow-credentials"] = "true"

    use_sse = (
        _is_sse_path(path)
        or "text/event-stream" in (media or "").lower()
        or "text/event-stream" in (request.headers.get("accept") or "").lower()
    )

    async def stream_bytes() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    if use_sse:
        out_headers.setdefault("cache-control", "no-cache, no-transform")
        out_headers.setdefault("connection", "keep-alive")
        out_headers.setdefault("x-accel-buffering", "no")
        return StreamingResponse(
            stream_bytes(),
            status_code=upstream_resp.status_code,
            media_type="text/event-stream",
            headers=out_headers,
        )

    content = await upstream_resp.aread()
    await upstream_resp.aclose()
    await client.aclose()
    return Response(
        content=content,
        status_code=upstream_resp.status_code,
        media_type=media,
        headers=out_headers,
    )
