"""共享 FastAPI 装配：微服务薄入口复用 CORS / 日志 / lifespan / 追踪 / 注册。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.trace_context import TraceLogFilter, TraceMiddleware
from app.services.github_client import github_client
from app.services.rag_service import rag_service
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] trace=%(trace_id)s span=%(span_id)s %(name)s: %(message)s",
        )
    for h in logging.root.handlers:
        # 避免重复挂同一 filter 类型
        if not any(isinstance(f, TraceLogFilter) for f in h.filters):
            h.addFilter(TraceLogFilter())


def add_cors(app: FastAPI) -> None:
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-trace-id", "x-span-id", "x-parent-span-id"],
    )


def make_lifespan(
    *,
    need_redis: bool = True,
    need_rag: bool = False,
    need_github: bool = False,
    register_as: str = "",
    advertise_url: str = "",
    register_meta: Optional[Dict[str, Any]] = None,
) -> Callable:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        hb_task = None
        if need_redis:
            await redis_service.connect()
        if need_rag:
            try:
                await rag_service.connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Postgres/RAG unavailable: %s", exc)
        name = (register_as or get_settings().service_name or "").strip()
        url = (advertise_url or get_settings().service_advertise_url or "").strip()
        if name and url:
            from app.core import service_registry

            try:
                hb_task = await service_registry.start_heartbeat_loop(
                    name, url, meta=register_meta or {"mode": get_settings().gateway_mode}
                )
                logger.info("service registered: %s → %s", name, url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("service register failed: %s", exc)
        yield
        if hb_task is not None:
            hb_task.cancel()
            try:
                from app.core import service_registry

                await service_registry.deregister(name)
            except Exception:  # noqa: BLE001
                pass
        if need_github:
            await github_client.close()
            try:
                from app.services.github_rpc import github_rpc

                await github_rpc.close()
            except Exception:  # noqa: BLE001
                pass
        if need_rag:
            try:
                await rag_service.close()
            except Exception:  # noqa: BLE001
                pass
        if need_redis:
            await redis_service.close()

    return lifespan


def create_service_app(
    title: str,
    *,
    need_redis: bool = True,
    need_rag: bool = False,
    need_github: bool = False,
    register_as: str = "",
    advertise_url: str = "",
    register_meta: Optional[Dict[str, Any]] = None,
    version: str = "1.0.0",
) -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title=title,
        version=version,
        lifespan=make_lifespan(
            need_redis=need_redis,
            need_rag=need_rag,
            need_github=need_github,
            register_as=register_as or settings.service_name,
            advertise_url=advertise_url or settings.service_advertise_url,
            register_meta=register_meta,
        ),
    )
    # 后加的中间件先执行：Trace 应包在最外（除 CORS）
    app.add_middleware(TraceMiddleware)
    add_cors(app)
    return app
