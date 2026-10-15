"""Deploy Key + git 推送：PAT 无 Contents 写权限时的落盘通道。

面试点：读写分离——PAT 只读元数据/文件；写操作走 Deploy Key（git SSH），
PR 若 API 无权限则回落 compare 链接让用户一键开 PR。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / ".data"
KEY_DIR = DATA_DIR / "ssh"
KEY_PRIVATE = KEY_DIR / "deploy_ed25519"
KEY_PUBLIC = KEY_DIR / "deploy_ed25519.pub"
REPOS_DIR = DATA_DIR / "repos"


class GitSSHError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


async def _run(cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="replace").strip()
    err = (err_b or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise GitSSHError(
            f"命令失败: {' '.join(cmd[:4])}…",
            detail=(err or out)[:800],
        )
    return out or err


def ensure_keypair() -> Dict[str, str]:
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_PRIVATE.exists() or not KEY_PUBLIC.exists():
        # ssh-keygen -N "" 空口令
        import subprocess

        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(KEY_PRIVATE),
                "-N",
                "",
                "-C",
                "github-agent-deploy-key",
            ],
            check=True,
            capture_output=True,
        )
        # Windows: 收紧私钥权限（尽量）
        try:
            os.chmod(KEY_PRIVATE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    pub = KEY_PUBLIC.read_text(encoding="utf-8").strip()
    return {
        "public_key": pub,
        "private_key_path": str(KEY_PRIVATE),
        "add_url_template": "https://github.com/{owner}/{repo}/settings/keys/new",
    }


def setup_info(owner: str, repo: str) -> Dict[str, Any]:
    info = ensure_keypair()
    return {
        "public_key": info["public_key"],
        "add_url": f"https://github.com/{owner}/{repo}/settings/keys/new",
        "instructions": [
            "打开下方链接（需仓库管理员）",
            "Title 填：github-agent",
            "Key 粘贴下方公钥",
            "勾选 Allow write access",
            "Add key 后回到本页点「检测 Deploy Key」",
        ],
        "configured": KEY_PRIVATE.exists(),
    }


def _ssh_env() -> dict:
    env = os.environ.copy()
    # 禁用交互；接受 github.com host key
    env["GIT_SSH_COMMAND"] = (
        f'ssh -i "{KEY_PRIVATE}" -o IdentitiesOnly=yes '
        f"-o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    )
    return env


async def verify_deploy_key(owner: str, repo: str) -> Dict[str, Any]:
    ensure_keypair()
    try:
        out = await _run(
            ["git", "ls-remote", f"git@github.com:{owner}/{repo}.git", "HEAD"],
            env=_ssh_env(),
        )
        ok = bool(out.strip())
        return {
            "ok": ok,
            "can_read": ok,
            "detail": "Deploy Key 可读仓库" if ok else "无法通过 SSH 读取仓库",
            "raw": out[:200],
        }
    except GitSSHError as e:
        return {
            "ok": False,
            "can_read": False,
            "detail": "Deploy Key 未生效：请确认已添加并勾选 Allow write access",
            "raw": e.detail[:300],
        }


def _work_dir(owner: str, repo: str) -> Path:
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    return REPOS_DIR / f"{owner}__{repo}"


async def _ensure_clone(
    owner: str,
    repo: str,
    read_token: str = "",
) -> Path:
    """确保本地有可推送的 clone（SSH 优先，失败则 HTTPS+PAT 只读再改 remote）。"""
    ensure_keypair()
    work = _work_dir(owner, repo)
    env = _ssh_env()
    ssh_url = f"git@github.com:{owner}/{repo}.git"

    if work.exists() and not (work / ".git").exists():
        shutil.rmtree(work, ignore_errors=True)

    if not (work / ".git").exists():
        try:
            await _run(["git", "clone", "--depth", "1", ssh_url, str(work)], env=env)
        except GitSSHError:
            if not read_token:
                raise
            https = f"https://x-access-token:{quote(read_token)}@github.com/{owner}/{repo}.git"
            await _run(["git", "clone", "--depth", "1", https, str(work)])
            await _run(["git", "remote", "set-url", "origin", ssh_url], cwd=work)
    return work


async def create_branch_via_ssh(
    owner: str,
    repo: str,
    branch: str,
    from_branch: str = "main",
    read_token: str = "",
) -> Dict[str, Any]:
    """PAT 无写权限时：用 Deploy Key 从 from_branch 推出新分支。"""
    from app.services.write_gate import require_github_write

    require_github_write(operation="create_branch_via_ssh", repo=f"{owner}/{repo}")
    work = await _ensure_clone(owner, repo, read_token=read_token)
    env = _ssh_env()
    await _run(["git", "fetch", "origin", from_branch, "--depth", "1"], cwd=work, env=env)
    await _run(["git", "checkout", "-B", branch, f"origin/{from_branch}"], cwd=work, env=env)
    try:
        await _run(["git", "push", "-u", "origin", branch], cwd=work, env=env)
    except GitSSHError as e:
        # 分支已存在视为成功（幂等）
        detail = (e.detail or "").lower()
        if "already exists" not in detail and "up-to-date" not in detail:
            raise GitSSHError(
                "Deploy Key 建分支失败：请确认 Key 已勾选 Allow write access",
                detail=e.detail,
            ) from e
    sha = await _run(["git", "rev-parse", "HEAD"], cwd=work, env=env)
    return {
        "ok": True,
        "ref": f"refs/heads/{branch}",
        "object": {"sha": sha},
        "via": "deploy_key",
        "detail": f"已通过 Deploy Key 创建分支 {branch}",
    }


async def push_file_via_ssh(
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str,
    base_branch: str,
    target_branch: str,
    read_token: str = "",
) -> Dict[str, Any]:
    """用 Deploy Key 推送文件到 target_branch，返回 commit / compare URL。"""
    from app.services.write_gate import require_github_write

    require_github_write(operation="push_file_via_ssh", repo=f"{owner}/{repo}")
    work = await _ensure_clone(owner, repo, read_token=read_token)
    env = _ssh_env()

    # 同步 base
    await _run(["git", "fetch", "origin", base_branch, "--depth", "1"], cwd=work, env=env)
    await _run(["git", "checkout", "-B", "tmp-base", f"origin/{base_branch}"], cwd=work, env=env)
    await _run(["git", "checkout", "-B", target_branch], cwd=work, env=env)

    file_path = work / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    # 统一换行，避免无意义 diff
    text = content.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    file_path.write_text(text, encoding="utf-8")

    await _run(["git", "add", "--", path], cwd=work, env=env)
    # 无变更也允许（幂等）
    status = await _run(["git", "status", "--porcelain"], cwd=work, env=env)
    commit_sha = ""
    if status.strip():
        await _run(
            [
                "git",
                "-c",
                "user.email=github-agent@local",
                "-c",
                "user.name=GitHub Agent",
                "commit",
                "-m",
                message,
            ],
            cwd=work,
            env=env,
        )
        commit_sha = await _run(["git", "rev-parse", "HEAD"], cwd=work, env=env)
    else:
        commit_sha = await _run(["git", "rev-parse", "HEAD"], cwd=work, env=env)

    try:
        await _run(["git", "push", "-u", "origin", target_branch], cwd=work, env=env)
    except GitSSHError as e:
        raise GitSSHError(
            "Deploy Key 推送失败：请确认 Key 已勾选 Allow write access",
            detail=e.detail,
        ) from e

    compare = (
        f"https://github.com/{owner}/{repo}/compare/{base_branch}...{target_branch}"
        f"?quick_pull=1&title={quote(message[:80])}"
    )
    return {
        "ok": True,
        "branch": target_branch,
        "commit_sha": commit_sha,
        "html_url": f"https://github.com/{owner}/{repo}/tree/{target_branch}/{path}",
        "pr_url": compare,
        "pr_number": None,
        "via": "deploy_key",
        "detail": f"已通过 Deploy Key 推送到 {target_branch}；打开链接即可一键创建 PR",
    }
