"""GitHub Token 诊断、热更新与 Deploy Key 写通道（演示环境）。"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.security_auth import require_operator
from app.services import audit_service, git_ssh_writer
from app.services.github_client import github_client

router = APIRouter(prefix="/api/github", tags=["github-setup"])


class TokenUpdateRequest(BaseModel):
    token: str = Field(..., min_length=10)
    repo: str = Field(default="guo-mz/-", description="用于探测写权限的 owner/repo")


def _env_paths() -> list[Path]:
    backend_env = Path(__file__).resolve().parents[2] / ".env"
    root_env = Path(__file__).resolve().parents[3] / ".env"
    paths = [backend_env]
    if root_env != backend_env:
        paths.append(root_env)
    return paths


def _write_token_to_env(token: str) -> list[str]:
    updated: list[str] = []
    for path in _env_paths():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^GITHUB_TOKEN=.*$", text, flags=re.M):
            text = re.sub(r"^GITHUB_TOKEN=.*$", f"GITHUB_TOKEN={token}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\nGITHUB_TOKEN={token}\n"
        path.write_text(text, encoding="utf-8")
        updated.append(str(path))
    return updated


def _split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(400, "repo 必须是 owner/name")
    return parts[0], parts[1]


@router.get("/permissions")
async def github_permissions(
    repo: str = "guo-mz/-",
    _auth: None = Depends(require_operator),
):
    owner, name = _split_repo(repo)
    probe = await github_client.probe_permissions(owner, name)
    deploy = await git_ssh_writer.verify_deploy_key(owner, name)
    probe["deploy_key_ok"] = bool(deploy.get("ok"))
    probe["can_write"] = bool(probe.get("can_create_branch") or deploy.get("ok"))
    if not probe.get("can_create_branch") and deploy.get("ok"):
        probe["detail"] = "PAT 无写权限，但 Deploy Key 可用——保存将走 git SSH 推送"
        probe["ok"] = True
    return probe


@router.post("/token")
async def update_github_token(
    body: TokenUpdateRequest,
    _auth: None = Depends(require_operator),
):
    token = body.token.strip()
    if not (token.startswith("ghp_") or token.startswith("github_pat_") or token.startswith("gho_")):
        raise HTTPException(400, "Token 格式不对，应以 ghp_ / github_pat_ 开头")

    audit_service.record_security_event(
        action="token_config",
        result="allowed",
        reason="operator_authenticated",
        operation="update_github_token",
        repo=body.repo,
    )
    paths = _write_token_to_env(token)
    get_settings.cache_clear()
    await github_client.set_token(token)

    owner, name = _split_repo(body.repo)
    probe = await github_client.probe_permissions(owner, name)
    return {
        "ok": bool(probe.get("can_create_branch")),
        "updated_files": paths,
        "probe": probe,
        "message": (
            "Token 已热更新，写权限正常"
            if probe.get("can_create_branch")
            else "Token 已写入并热更新，但仍无写权限：请确认 Contents / Pull requests 为 Read and write，或改用 Deploy Key"
        ),
    }


@router.get("/deploy-key")
async def get_deploy_key(
    repo: str = "guo-mz/-",
    _auth: None = Depends(require_operator),
):
    owner, name = _split_repo(repo)
    info = git_ssh_writer.setup_info(owner, name)
    verify = await git_ssh_writer.verify_deploy_key(owner, name)
    info["verify"] = verify
    return info


@router.post("/deploy-key/verify")
async def verify_deploy_key(
    repo: str = "guo-mz/-",
    _auth: None = Depends(require_operator),
):
    owner, name = _split_repo(repo)
    return await git_ssh_writer.verify_deploy_key(owner, name)
