"""GitHub REST API 封装 —— 真实读写仓库；含限流降级与 SHA 乐观锁。

面试点：
- update_file 必须带 SHA，否则覆盖他人提交
- 429 / secondary rate limit → 读 Retry-After 退避
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubAPIError(Exception):
    def __init__(self, status: int, message: str, conflict: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.conflict = conflict


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        settings = get_settings()
        self.token = token or settings.github_token
        self._client: Optional[httpx.AsyncClient] = None

    async def set_token(self, token: str) -> None:
        """热更新 Token（本地配置页写入后立即生效）。"""
        self.token = (token or "").strip()
        await self.close()
        self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-agent/1.0",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API,
                headers=headers,
                timeout=60.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def probe_permissions(self, owner: str, repo: str) -> Dict[str, Any]:
        """探测读/写能力，供前端展示。"""
        out: Dict[str, Any] = {
            "ok": True,
            "login": "",
            "token_present": bool(self.token),
            "token_type": (
                "fine-grained"
                if (self.token or "").startswith("github_pat_")
                else "classic"
                if (self.token or "").startswith("ghp_")
                else "unknown"
            ),
            "can_read_repo": False,
            "can_create_branch": False,
            "detail": "",
        }
        if not self.token:
            out["ok"] = False
            out["detail"] = "未配置 GITHUB_TOKEN"
            return out
        try:
            user = await self.request("GET", "/user")
            out["login"] = user.get("login") or ""
        except GitHubAPIError as e:
            out["ok"] = False
            out["detail"] = f"Token 无效或过期：{e.message[:200]}"
            return out
        try:
            await self.request("GET", f"/repos/{owner}/{repo}")
            out["can_read_repo"] = True
        except GitHubAPIError as e:
            out["ok"] = False
            out["detail"] = f"无法读取仓库 {owner}/{repo}：{e.message[:200]}"
            return out
        # 写探测：创建临时分支再删除
        try:
            ref = await self.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/main")
            sha = ref["object"]["sha"]
            name = f"agent/perm-probe-{int(time.time()) % 100000}"
            await self.request(
                "POST",
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{name}", "sha": sha},
            )
            out["can_create_branch"] = True
            try:
                await self.request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{name}")
            except GitHubAPIError:
                pass
            out["detail"] = "读/写权限正常，可以保存上传与提 PR"
        except GitHubAPIError as e:
            out["ok"] = False
            out["can_create_branch"] = False
            if e.status == 403:
                out["detail"] = (
                    "Token 无写权限（建分支 403）。请用 Fine-grained Token，"
                    f"仓库选 {owner}/{repo}，Permissions → Contents: Read and write，"
                    "Pull requests: Read and write；保存后粘贴到本页并「应用 Token」。"
                )
            else:
                out["detail"] = f"建分支失败：{e.message[:300]}"
        return out

    async def _throttle(self) -> None:
        settings = get_settings()
        result = await redis_service.rate_limit(
            "github_api",
            limit=settings.rate_limit_github_per_hour,
            window=3600,
        )
        if not result.allowed:
            logger.warning("GitHub rate limited, sleep %ss", result.retry_after)
            await asyncio.sleep(min(result.retry_after or 1, 30))

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> Any:
        await self._throttle()
        client = await self._get_client()
        resp = await client.request(method, path, params=params, json=json)
        if resp.status_code == 409:
            raise GitHubAPIError(409, resp.text, conflict=True)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            await asyncio.sleep(retry_after)
            resp = await client.request(method, path, params=params, json=json)
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text)
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def list_issues(self, owner: str, repo: str, state: str = "open", per_page: int = 20) -> List[dict]:
        return await self.request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page},
        )

    async def get_issue(self, owner: str, repo: str, number: int) -> dict:
        return await self.request("GET", f"/repos/{owner}/{repo}/issues/{number}")

    async def search_code(self, query: str, owner: str = "", repo: str = "") -> dict:
        q = query
        if owner and repo:
            q = f"{query} repo:{owner}/{repo}"
        return await self.request("GET", "/search/code", params={"q": q, "per_page": 10})

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "main") -> dict:
        data = await self.request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
        )
        if isinstance(data, list):
            return {"type": "dir", "entries": data}
        content_b64 = data.get("content", "")
        encoding = data.get("encoding", "base64")
        text = ""
        if encoding == "base64" and content_b64:
            text = base64.b64decode(content_b64.replace("\n", "")).decode("utf-8", errors="replace")
        return {
            "type": "file",
            "path": data.get("path"),
            "sha": data.get("sha"),
            "size": data.get("size"),
            "content": text,
            "html_url": data.get("html_url"),
        }

    async def get_ref_sha(self, owner: str, repo: str, branch: str) -> str:
        data = await self.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        return data["object"]["sha"]

    async def create_branch(self, owner: str, repo: str, branch: str, from_branch: str = "main") -> dict:
        from app.services.write_gate import require_github_write

        require_github_write(operation="create_branch", repo=f"{owner}/{repo}")
        sha = await self.get_ref_sha(owner, repo, from_branch)
        return await self.request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    async def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str,
    ) -> dict:
        """必须带 SHA（乐观锁）；409 → conflict=True 供条件边重试。"""
        from app.services.write_gate import require_github_write

        require_github_write(operation="update_file", repo=f"{owner}/{repo}")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        payload: dict = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        # 新文件无 sha；已有文件必须带乐观锁 sha
        if sha:
            payload["sha"] = sha
        return await self.request(
            "PUT",
            f"/repos/{owner}/{repo}/contents/{path}",
            json=payload,
        )

    async def list_pulls(
        self,
        owner: str,
        repo: str,
        state: str = "open",
        per_page: int = 20,
    ) -> List[dict]:
        return await self.request(
            "GET",
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": per_page, "sort": "updated"},
        )

    async def get_pull(self, owner: str, repo: str, number: int) -> dict:
        return await self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    async def list_pull_files(self, owner: str, repo: str, number: int, per_page: int = 30) -> List[dict]:
        return await self.request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{number}/files",
            params={"per_page": per_page},
        )

    async def list_commits(
        self,
        owner: str,
        repo: str,
        sha: str = "main",
        per_page: int = 15,
        path: str = "",
    ) -> List[dict]:
        params: Dict[str, Any] = {"sha": sha, "per_page": per_page}
        if path:
            params["path"] = path
        return await self.request("GET", f"/repos/{owner}/{repo}/commits", params=params)

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
    ) -> dict:
        from app.services.write_gate import require_github_write

        require_github_write(operation="create_issue", repo=f"{owner}/{repo}")
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        return await self.request("POST", f"/repos/{owner}/{repo}/issues", json=payload)

    async def create_pr(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict:
        from app.services.write_gate import require_github_write

        require_github_write(operation="create_pr", repo=f"{owner}/{repo}")
        return await self.request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )

    async def add_comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        from app.services.write_gate import require_github_write

        require_github_write(operation="add_comment", repo=f"{owner}/{repo}")
        return await self.request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )

    async def merge_pr(
        self,
        owner: str,
        repo: str,
        number: int,
        merge_method: str = "squash",
    ) -> dict:
        from app.services.write_gate import require_github_write

        require_github_write(operation="merge_pr", repo=f"{owner}/{repo}")
        return await self.request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json={"merge_method": merge_method},
        )

    async def get_repo_tree(self, owner: str, repo: str, branch: str = "main") -> dict:
        ref = await self.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        sha = ref["object"]["sha"]
        return await self.request(
            "GET",
            f"/repos/{owner}/{repo}/git/trees/{sha}",
            params={"recursive": "1"},
        )

    async def create_or_update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
        sha: Optional[str] = None,
    ) -> dict:
        """空仓库首次写入可不带 sha；已有文件则带 sha。"""
        from app.services.write_gate import require_github_write

        require_github_write(operation="create_or_update_file", repo=f"{owner}/{repo}")
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        body: Dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        return await self.request(
            "PUT",
            f"/repos/{owner}/{repo}/contents/{path}",
            json=body,
        )

    async def compare(self, owner: str, repo: str, base: str, head: str) -> dict:
        return await self.request("GET", f"/repos/{owner}/{repo}/compare/{base}...{head}")


github_client = GitHubClient()
