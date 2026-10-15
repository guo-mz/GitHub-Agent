"""本地单操作员认证 —— 共享 OPERATOR_API_TOKEN，非多用户 IAM。

默认失败关闭：未配置 Token 且未显式 ALLOW_INSECURE_NO_AUTH=true 时拒绝敏感操作。
"""

from __future__ import annotations

import hmac
import secrets
from typing import Optional

from fastapi import Header, HTTPException, Request

from app.config import get_settings
from app.services import audit_service


def _extract_token(
    authorization: Optional[str],
    x_operator_token: Optional[str],
) -> str:
    if x_operator_token and x_operator_token.strip():
        return x_operator_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def auth_mode() -> str:
    """configured | insecure_open | fail_closed"""
    s = get_settings()
    token = (s.operator_api_token or "").strip()
    if token:
        return "configured"
    if s.allow_insecure_no_auth:
        return "insecure_open"
    return "fail_closed"


def verify_operator_token(
    *,
    authorization: Optional[str] = None,
    x_operator_token: Optional[str] = None,
    action: str = "sensitive",
    request_path: str = "",
) -> None:
    s = get_settings()
    expected = (s.operator_api_token or "").strip()
    provided = _extract_token(authorization, x_operator_token)

    if not expected:
        if s.allow_insecure_no_auth:
            audit_service.record_security_event(
                action="auth",
                result="allowed",
                reason="insecure_no_auth",
                operation=action,
                extra={"path": request_path[:200]},
            )
            return
        audit_service.record_security_event(
            action="auth",
            result="denied",
            reason="operator_token_not_configured",
            operation=action,
            extra={"path": request_path[:200]},
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "未配置 OPERATOR_API_TOKEN：敏感操作默认拒绝。"
                "请在 backend/.env 设置 OPERATOR_API_TOKEN，"
                "或仅本地调试时设置 ALLOW_INSECURE_NO_AUTH=true（不安全）。"
            ),
        )

    if not provided or not hmac.compare_digest(provided, expected):
        audit_service.record_security_event(
            action="auth",
            result="denied",
            reason="invalid_operator_token",
            operation=action,
            extra={"path": request_path[:200]},
        )
        raise HTTPException(
            status_code=401,
            detail="未认证：请提供有效的 X-Operator-Token 或 Authorization: Bearer <token>",
        )


async def require_operator(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_operator_token: Optional[str] = Header(default=None, alias="X-Operator-Token"),
) -> None:
    verify_operator_token(
        authorization=authorization,
        x_operator_token=x_operator_token,
        action=request.url.path,
        request_path=str(request.url.path),
    )


def new_operator_token_hint() -> str:
    """生成示例强度的随机串（仅文档/运维提示，不写入配置）。"""
    return secrets.token_urlsafe(32)
