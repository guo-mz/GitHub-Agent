"""FastAPI 单体入口 —— 生命周期内连接 Redis / Postgres，挂载全部路由。

微服务模式下请分别启动 services/agent_svc、repo_svc、gateway；本文件保持可单进程运行。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import approval, auth, chat, eval as eval_api, github_setup, llm_usage, repos, webhook
from app.config import get_settings
from app.models.schemas import HealthResponse
from app.services.github_client import github_client
from app.services.rag_service import rag_service
from app.services.redis_service import redis_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] trace=%(trace_id)s %(name)s: %(message)s",
)


class TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = "-"
        return True


for h in logging.root.handlers:
    h.addFilter(TraceFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 本地无 Docker 时允许降级启动（Redis→内存，Postgres→RAG 不可用）
    await redis_service.connect()
    try:
        await rag_service.connect()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Postgres/RAG unavailable: %s", exc)
    yield
    await github_client.close()
    try:
        await rag_service.close()
    except Exception:  # noqa: BLE001
        pass
    await redis_service.close()


app = FastAPI(
    title="GitHub Intelligent R&D Assistant",
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(approval.router)
app.include_router(webhook.router)
app.include_router(auth.router)
app.include_router(github_setup.router)
app.include_router(repos.router)
app.include_router(eval_api.router)
app.include_router(llm_usage.router)


@app.get("/health", response_model=HealthResponse)
async def health():
    from app.core.checkpointer import get_checkpointer, get_checkpointer_mode

    await rag_service.connect()
    try:
        await get_checkpointer()
    except Exception:  # noqa: BLE001
        pass
    from app.config import get_settings
    from app.services.transformer_rerank import transformer_reranker

    settings = get_settings()
    from app.core.runtime_mode import runtime_persistence_status
    from app.core.security_auth import auth_mode

    persistence = runtime_persistence_status()
    # 透明暴露模式：memory 不得被前端/答辩误读为 Redis 持久化
    return HealthResponse(
        status="ok",
        redis=await redis_service.ping(),
        postgres=await rag_service.postgres_alive(),
        rag=await rag_service.ping(),
        rag_mode=rag_service.mode,
        checkpointer=get_checkpointer_mode(),
        rerank=transformer_reranker.health_label(
            enabled=bool(settings.transformer_rerank_enabled)
        ),
        mode="monolith",
        persistence=persistence,
        auth_mode=auth_mode(),
    )
