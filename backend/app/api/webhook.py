"""GitHub Webhook —— 签名校验默认失败关闭；未验证事件不得入队或触发 Agent。"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import get_settings
from app.models.schemas import WebhookAck
from app.services import audit_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhook", tags=["webhook"])


def verify_webhook_signature(secret: str, body: bytes, signature: str) -> tuple[bool, str]:
    """返回 (ok, reason)。secret 为空时由调用方按环境策略决定是否宽松。"""
    if not signature or not str(signature).strip():
        return False, "signature_missing"
    if not str(signature).startswith("sha256="):
        return False, "signature_format_invalid"
    if not secret:
        return False, "secret_not_configured"
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    if not hmac.compare_digest(expected, str(signature).strip()):
        return False, "signature_mismatch"
    return True, "ok"


# 兼容旧测试名
def _verify_signature(secret: str, body: bytes, signature: str) -> bool:
    ok, _ = verify_webhook_signature(secret, body, signature)
    if not secret:
        # 旧行为曾跳过；现由路由层显式处理 insecure 开关
        return False
    return ok


@router.post("/github", response_model=WebhookAck)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
):
    settings = get_settings()
    body = await request.body()
    secret = (settings.github_webhook_secret or "").strip()

    if not secret:
        if settings.allow_insecure_webhook:
            audit_service.record_security_event(
                action="webhook",
                result="allowed",
                reason="insecure_webhook_no_secret",
                operation=x_github_event or "unknown",
            )
            logger.warning(
                "ALLOW_INSECURE_WEBHOOK=true：未配置 GITHUB_WEBHOOK_SECRET，已跳过签名校验（仅限本地调试）"
            )
        else:
            audit_service.record_security_event(
                action="webhook",
                result="denied",
                reason="webhook_secret_not_configured",
                operation=x_github_event or "unknown",
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    "未配置 GITHUB_WEBHOOK_SECRET：Webhook 默认拒绝。"
                    "请配置密钥，或仅本地调试时设置 ALLOW_INSECURE_WEBHOOK=true（不安全）。"
                ),
            )
    else:
        ok, reason = verify_webhook_signature(secret, body, x_hub_signature_256)
        if not ok:
            audit_service.record_security_event(
                action="webhook",
                result="denied",
                reason=reason,
                operation=x_github_event or "unknown",
            )
            raise HTTPException(status_code=401, detail=f"invalid signature:{reason}")

    # 验证通过后仅确认收到；本阶段不入队、不触发 Agent 执行
    import json as _json

    try:
        payload = _json.loads(body.decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        payload = {}
    action = payload.get("action") if isinstance(payload, dict) else None
    logger.info("GitHub webhook event=%s action=%s", x_github_event, action)

    return WebhookAck(ok=True, detail=f"received {x_github_event} (no enqueue)")
