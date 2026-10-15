"""Repo 内部 RPC：Agent 经 HTTP 调 GitHub 能力（服务间 RPC 演示）。

鉴权：X-Internal-Token 与配置一致（hmac.compare_digest）；未配置/弱默认 fail-closed。
单体可不挂此路由。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.internal_rpc_auth import resolve_internal_rpc_token, tokens_match
from app.services.github_client import GitHubAPIError, github_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/v1/github", tags=["internal-rpc"])

# 只读白名单：写方法一律拒绝（即使持有 internal token）
_ALLOWED = {
    "get_file_content",
    "get_repo_tree",
    "list_commits",
    "list_issues",
    "get_issue",
    "list_pulls",
    "get_pull",
    "list_pull_files",
    "compare",
    "search_code",
    "get_ref_sha",
}
_WRITE_DENIED = {
    "create_or_update_file",
    "update_file",
    "create_branch",
    "create_pr",
    "create_issue",
    "add_comment",
    "merge_pr",
}
_METHOD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_ARGS_KEYS = 32


class RpcRequest(BaseModel):
    method: str = Field(..., min_length=1, max_length=64)
    args: Dict[str, Any] = Field(default_factory=dict)


class RpcResponse(BaseModel):
    ok: bool
    result: Any = None
    error: Optional[str] = None
    status: Optional[int] = None


def _check_token(token: Optional[str]) -> None:
    """内部 RPC：未配置 / 弱默认 / 过短 / 错误 Token 一律 401。"""
    expected = resolve_internal_rpc_token()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="INTERNAL_RPC_TOKEN 未配置或不符合要求：内部 RPC 默认拒绝",
        )
    if not tokens_match(token, expected):
        raise HTTPException(status_code=401, detail="invalid internal token")


def _validate_args(args: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="bad args: must be object")
    if len(args) > _MAX_ARGS_KEYS:
        raise HTTPException(status_code=400, detail="bad args: too many keys")
    for key in args:
        if not isinstance(key, str) or not _METHOD_RE.match(key):
            raise HTTPException(status_code=400, detail="bad args: invalid key")
    return args


@router.post("/call", response_model=RpcResponse)
async def github_rpc_call(
    body: RpcRequest,
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
):
    _check_token(x_internal_token)
    method = (body.method or "").strip()
    if not _METHOD_RE.match(method):
        raise HTTPException(status_code=400, detail="method not allowed")
    if method in _WRITE_DENIED:
        raise HTTPException(
            status_code=403,
            detail=f"write method denied by security policy: {method}",
        )
    if method not in _ALLOWED:
        raise HTTPException(status_code=400, detail=f"method not allowed: {method}")
    fn = getattr(github_client, method, None)
    if fn is None or not callable(fn):
        raise HTTPException(status_code=400, detail=f"unknown method: {method}")
    args = _validate_args(body.args or {})
    try:
        result = await fn(**args)
        return RpcResponse(ok=True, result=result)
    except GitHubAPIError as exc:
        logger.warning("internal github rpc %s failed: status=%s", method, exc.status)
        return RpcResponse(ok=False, error=exc.message, status=exc.status)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"bad args: {exc}") from exc
    except Exception:  # noqa: BLE001
        logger.exception("internal github rpc crash")
        return RpcResponse(ok=False, error="internal error", status=500)


@router.get("/health")
async def rpc_health():
    return {"ok": True, "service": "repo-github-rpc"}
