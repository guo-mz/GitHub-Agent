"""Repo 微服务：仓库/RAG/OAuth + 内部 GitHub RPC；Postgres 归属本服务。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
for p in (str(_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.api import auth, github_setup, internal_github, repos, webhook  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.factory import create_service_app  # noqa: E402
from app.models.schemas import HealthResponse  # noqa: E402
from app.services.rag_service import rag_service  # noqa: E402
from app.services.redis_service import redis_service  # noqa: E402

_settings = get_settings()
_advertise = (_settings.service_advertise_url or f"http://127.0.0.1:8012").rstrip("/")

app = create_service_app(
    "GitHub Repo Service",
    need_redis=True,
    need_rag=True,
    need_github=True,
    register_as="repo",
    advertise_url=_advertise,
    register_meta={"role": "repo", "owns": ["postgres", "rag"]},
)

app.include_router(repos.router)
app.include_router(auth.router)
app.include_router(webhook.router)
app.include_router(github_setup.router)
app.include_router(internal_github.router)


@app.get("/health", response_model=HealthResponse)
async def health():
    from app.config import get_settings
    from app.services.transformer_rerank import transformer_reranker

    await rag_service.connect()
    return HealthResponse(
        status="ok",
        redis=await redis_service.ping(),
        postgres=await rag_service.postgres_alive(),
        rag=await rag_service.ping(),
        rag_mode=rag_service.mode,
        checkpointer="none",
        rerank=transformer_reranker.health_label(
            enabled=bool(get_settings().transformer_rerank_enabled)
        ),
        mode="repo",
        services={"github_rpc": True},
    )
