"""Agent 微服务：对话 + 审批；GitHub 经 Repo RPC；Redis DB 独立。"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
for p in (str(_ROOT), str(_BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.api import approval, chat, eval as eval_api, llm_usage  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.factory import create_service_app  # noqa: E402
from app.models.schemas import HealthResponse  # noqa: E402
from app.services.redis_service import redis_service  # noqa: E402

_settings = get_settings()
_advertise = (_settings.service_advertise_url or f"http://127.0.0.1:8011").rstrip("/")

app = create_service_app(
    "GitHub Agent Service",
    need_redis=True,
    need_rag=False,
    need_github=True,
    register_as="agent",
    advertise_url=_advertise,
    register_meta={"rpc": _settings.repo_rpc_enabled, "role": "agent"},
)

app.include_router(chat.router)
app.include_router(approval.router)
app.include_router(eval_api.router)
app.include_router(llm_usage.router)


@app.get("/health", response_model=HealthResponse)
async def health():
    from app.core.checkpointer import get_checkpointer, get_checkpointer_mode

    try:
        await get_checkpointer()
    except Exception:  # noqa: BLE001
        pass
    return HealthResponse(
        status="ok",
        redis=await redis_service.ping(),
        postgres=False,
        rag=False,
        rag_mode="none",
        checkpointer=get_checkpointer_mode(),
        mode="agent",
        services={"repo_rpc": _settings.repo_rpc_enabled},
    )
