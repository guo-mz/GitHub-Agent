"""GitHub OAuth —— 跳转授权 + 回调换 token。"""

from __future__ import annotations

import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.services.redis_service import redis_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"


@router.get("/github")
async def github_oauth_start():
    settings = get_settings()
    if not settings.github_client_id:
        raise HTTPException(status_code=503, detail="GITHUB_CLIENT_ID 未配置")
    state = secrets.token_urlsafe(16)
    await redis_service.cache_session(f"oauth:{state}", {"state": state}, ttl=600)
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": "repo read:user",
        "state": state,
    }
    return RedirectResponse(f"{GITHUB_AUTHORIZE}?{urlencode(params)}")


@router.get("/callback")
async def github_oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
):
    settings = get_settings()
    cached = await redis_service.get_session(f"oauth:{state}")
    if not cached:
        raise HTTPException(status_code=400, detail="invalid oauth state")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GITHUB_TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_oauth_redirect_uri,
            },
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail=str(token_data))

        user_resp = await client.get(
            GITHUB_USER,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        user = user_resp.json()

    # 前端用 hash 接收（生产应改用 httpOnly cookie）
    frontend = settings.cors_origin_list[0] if settings.cors_origin_list else "http://localhost:5173"
    q = urlencode({"login": user.get("login", ""), "token": access_token})
    return RedirectResponse(f"{frontend}/chat?{q}")


@router.get("/status")
async def auth_status(token: Optional[str] = None):
    if not token:
        return {"authenticated": False}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GITHUB_USER,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        if resp.status_code >= 400:
            return {"authenticated": False}
        return {"authenticated": True, "user": resp.json().get("login")}
