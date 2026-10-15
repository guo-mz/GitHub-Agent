"""Agent 侧 GitHub RPC 客户端：HTTP → Repo `/internal/v1/github/call`。"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.core.trace_context import outbound_trace_headers
from app.services.github_client import GitHubAPIError

logger = logging.getLogger(__name__)


class GitHubRPC:
    """方法名与 github_client 对齐，供工具 / review 统一切换。"""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    def _base(self) -> str:
        return get_settings().repo_rpc_url.rstrip("/")

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def call(self, method: str, **args: Any) -> Any:
        from app.core.internal_rpc_auth import resolve_internal_rpc_token

        headers = {
            "Content-Type": "application/json",
            **outbound_trace_headers(),
        }
        token = resolve_internal_rpc_token()
        if not token:
            raise GitHubAPIError(
                401,
                "INTERNAL_RPC_TOKEN 未配置或不符合要求：拒绝发起内部 RPC",
            )
        headers["X-Internal-Token"] = token
        client = await self._http()
        url = f"{self._base()}/internal/v1/github/call"
        resp = await client.post(url, json={"method": method, "args": args}, headers=headers)
        if resp.status_code >= 400:
            raise GitHubAPIError(resp.status_code, resp.text[:500])
        data = resp.json()
        if not data.get("ok"):
            raise GitHubAPIError(int(data.get("status") or 502), str(data.get("error") or "rpc failed"))
        return data.get("result")

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "main"):
        return await self.call("get_file_content", owner=owner, repo=repo, path=path, ref=ref)

    async def get_repo_tree(self, owner: str, repo: str, branch: str = "main"):
        return await self.call("get_repo_tree", owner=owner, repo=repo, branch=branch)

    async def update_file(self, owner, repo, path, content, message, branch, sha):
        return await self.call(
            "update_file",
            owner=owner,
            repo=repo,
            path=path,
            content=content,
            message=message,
            branch=branch,
            sha=sha,
        )

    async def create_or_update_file(self, owner, repo, path, content, message, branch="main", sha=None):
        return await self.call(
            "create_or_update_file",
            owner=owner,
            repo=repo,
            path=path,
            content=content,
            message=message,
            branch=branch,
            sha=sha,
        )

    async def create_branch(self, owner, repo, branch, from_branch="main"):
        return await self.call(
            "create_branch", owner=owner, repo=repo, branch=branch, from_branch=from_branch
        )

    async def create_pr(self, owner, repo, title, body, head, base="main"):
        return await self.call(
            "create_pr", owner=owner, repo=repo, title=title, body=body, head=head, base=base
        )

    async def list_commits(self, owner, repo, sha="main", per_page=10, path=""):
        return await self.call(
            "list_commits", owner=owner, repo=repo, sha=sha, per_page=per_page, path=path or ""
        )

    async def list_issues(self, owner, repo, state="open", per_page=10):
        return await self.call("list_issues", owner=owner, repo=repo, state=state, per_page=per_page)

    async def get_issue(self, owner, repo, number: int):
        return await self.call("get_issue", owner=owner, repo=repo, number=number)

    async def list_pulls(self, owner, repo, state="open", per_page=10):
        return await self.call("list_pulls", owner=owner, repo=repo, state=state, per_page=per_page)

    async def get_pull(self, owner, repo, number: int):
        return await self.call("get_pull", owner=owner, repo=repo, number=number)

    async def list_pull_files(self, owner, repo, number: int, per_page=30):
        return await self.call(
            "list_pull_files", owner=owner, repo=repo, number=number, per_page=per_page
        )

    async def create_issue(self, owner, repo, title, body="", labels=None):
        return await self.call(
            "create_issue",
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            labels=labels or [],
        )

    async def add_comment(self, owner, repo, number, body):
        return await self.call("add_comment", owner=owner, repo=repo, number=number, body=body)

    async def merge_pr(self, owner, repo, number, merge_method="squash"):
        return await self.call(
            "merge_pr", owner=owner, repo=repo, number=number, merge_method=merge_method
        )

    async def compare(self, owner, repo, base, head):
        return await self.call("compare", owner=owner, repo=repo, base=base, head=head)

    async def search_code(self, query: str, owner: str = "", repo: str = ""):
        return await self.call("search_code", query=query, owner=owner, repo=repo)

    async def get_ref_sha(self, owner, repo, branch):
        return await self.call("get_ref_sha", owner=owner, repo=repo, branch=branch)


github_rpc = GitHubRPC()


def github_backend():
    """工具层统一入口：微服开 RPC，否则直连 GitHub。"""
    if get_settings().repo_rpc_enabled:
        return github_rpc
    from app.services.github_client import github_client

    return github_client
