"""GitHub 工具集：@tool + Pydantic schema，docstring 供 LLM 选型。

面试点：
- update_file 强制 sha（乐观锁）
- create_pr / merge_pr 在图中走 interrupt，工具本身仍可调用真实 API
- 幂等锁包住 create_pr，防重复 PR
"""

from __future__ import annotations

import json
from typing import List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.idempotency import build_idempotency_key
from app.services import git_ssh_writer
from app.services.github_client import GitHubAPIError
from app.services.git_ssh_writer import GitSSHError
from app.services.github_rpc import github_backend
from app.services.redis_service import redis_service


def _split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2:
        raise ValueError("repo 必须是 owner/name 格式")
    return parts[0], parts[1]


def _is_pat_write_denied(exc: GitHubAPIError) -> bool:
    msg = (exc.message or "").lower()
    return exc.status == 403 or "personal access token" in msg or "resource not accessible" in msg


class ListIssuesInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    state: str = Field(default="open", description="open|closed|all")
    per_page: int = Field(default=10, ge=1, le=50)


class ListPRsInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    state: str = Field(default="open", description="open|closed|all")
    per_page: int = Field(default=10, ge=1, le=50)


class ListCommitsInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    sha: str = Field(default="main", description="分支或 commit SHA")
    per_page: int = Field(default=10, ge=1, le=30)
    path: str = Field(default="", description="可选：仅该文件路径的提交")


class CreateIssueInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    title: str = Field(..., description="Issue 标题")
    body: str = Field(default="", description="Issue 正文，可用 Markdown")
    labels: List[str] = Field(default_factory=list, description="标签名列表")


class CompareBranchesInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    base: str = Field(..., description="基准分支，如 main")
    head: str = Field(..., description="对比分支，如 feature/x")


class GetIssueInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    number: int = Field(..., description="Issue 或 PR 编号")


class SearchCodeInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    query: str = Field(..., description="GitHub code search 查询词")


class GetFileInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    path: str = Field(..., description="文件路径")
    ref: str = Field(default="main", description="分支或 commit SHA")


class CreateBranchInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    branch: str = Field(..., description="新分支名，勿使用 main/master")
    from_branch: str = Field(default="main", description="源分支")


class UpdateFileInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    path: str = Field(..., description="文件路径")
    content: str = Field(..., description="文件完整新内容")
    message: str = Field(..., description="commit message")
    branch: str = Field(..., description="目标分支（非受保护分支）")
    sha: str = Field(..., description="当前文件 blob SHA，乐观锁必填")


class CreatePRInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    title: str = Field(..., description="PR 标题")
    body: str = Field(default="", description="PR 描述")
    head: str = Field(..., description="源分支")
    base: str = Field(default="main", description="目标分支")


class AddCommentInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    number: int = Field(..., description="Issue/PR 编号")
    body: str = Field(..., description="评论正文")


class MergePRInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    number: int = Field(..., description="PR 编号")
    merge_method: str = Field(default="squash", description="merge|squash|rebase")


@tool("list_issues", args_schema=ListIssuesInput)
async def list_issues(repo: str, state: str = "open", per_page: int = 10) -> str:
    """列出仓库 Issues（不含 PR 过滤由调用方处理）。用于了解待办与 bug 列表。"""
    owner, name = _split_repo(repo)
    data = await github_backend().list_issues(owner, name, state=state, per_page=per_page)
    simplified = [
        {
            "number": i.get("number"),
            "title": i.get("title"),
            "state": i.get("state"),
            "user": (i.get("user") or {}).get("login"),
            "labels": [l.get("name") for l in i.get("labels", [])],
        }
        for i in data
        if "pull_request" not in i
    ]
    return json.dumps(simplified, ensure_ascii=False, indent=2)


@tool("get_issue", args_schema=GetIssueInput)
async def get_issue(repo: str, number: int) -> str:
    """获取单个 Issue/PR 的标题、正文、标签、状态。处理 issue #N 时先调用此工具。"""
    owner, name = _split_repo(repo)
    data = await github_backend().get_issue(owner, name, number)
    out = {
        "number": data.get("number"),
        "title": data.get("title"),
        "body": (data.get("body") or "")[:8000],
        "state": data.get("state"),
        "user": (data.get("user") or {}).get("login"),
        "labels": [l.get("name") for l in data.get("labels", [])],
        "html_url": data.get("html_url"),
        "is_pull_request": "pull_request" in data,
    }
    return json.dumps(out, ensure_ascii=False, indent=2)


@tool("search_code", args_schema=SearchCodeInput)
async def search_code(repo: str, query: str) -> str:
    """在指定仓库用 GitHub Code Search 搜索代码符号/字符串。"""
    owner, name = _split_repo(repo)
    data = await github_backend().search_code(query, owner=owner, repo=name)
    items = [
        {
            "path": i.get("path"),
            "name": i.get("name"),
            "sha": i.get("sha"),
            "url": i.get("html_url"),
        }
        for i in data.get("items", [])[:10]
    ]
    return json.dumps({"total": data.get("total_count"), "items": items}, ensure_ascii=False, indent=2)


@tool("get_file_content", args_schema=GetFileInput)
async def get_file_content(repo: str, path: str, ref: str = "main") -> str:
    """读取文件内容与 blob SHA。修改文件前必须调用以获取 sha。"""
    owner, name = _split_repo(repo)
    data = await github_backend().get_file_content(owner, name, path, ref=ref)
    return json.dumps(data, ensure_ascii=False, indent=2)


@tool("create_branch", args_schema=CreateBranchInput)
async def create_branch(repo: str, branch: str, from_branch: str = "main") -> str:
    """从 from_branch 创建新分支。写操作前先建特性分支，禁止直接写 main/master。"""
    owner, name = _split_repo(repo)
    try:
        data = await github_backend().create_branch(owner, name, branch, from_branch=from_branch)
        return json.dumps(
            {"ok": True, "ref": data.get("ref"), "sha": data.get("object", {}).get("sha")},
            ensure_ascii=False,
        )
    except GitHubAPIError as e:
        if not _is_pat_write_denied(e):
            return json.dumps({"ok": False, "error": e.message, "status": e.status})
        # PAT 无 Contents 写权限 → Deploy Key SSH 建分支
        try:
            data = await git_ssh_writer.create_branch_via_ssh(
                owner,
                name,
                branch,
                from_branch=from_branch,
                read_token=get_settings().github_token,
            )
            return json.dumps(
                {
                    "ok": True,
                    "ref": data.get("ref"),
                    "sha": (data.get("object") or {}).get("sha"),
                    "via": "deploy_key",
                    "detail": data.get("detail"),
                },
                ensure_ascii=False,
            )
        except GitSSHError as ssh_e:
            return json.dumps(
                {
                    "ok": False,
                    "error": ssh_e.message,
                    "detail": ssh_e.detail,
                    "status": 403,
                    "hint": "Deploy Key 需勾选 Allow write access",
                },
                ensure_ascii=False,
            )


@tool("update_file", args_schema=UpdateFileInput)
async def update_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str,
    sha: str,
) -> str:
    """在指定分支更新文件并 commit。必须提供当前文件 sha（乐观锁）；冲突返回 conflict=true。"""
    owner, name = _split_repo(repo)
    try:
        data = await github_backend().update_file(owner, name, path, content, message, branch, sha)
        commit = data.get("commit", {})
        return json.dumps(
            {
                "ok": True,
                "path": path,
                "branch": branch,
                "commit_sha": commit.get("sha"),
                "content_sha": (data.get("content") or {}).get("sha"),
                "html_url": commit.get("html_url"),
            },
            ensure_ascii=False,
        )
    except GitHubAPIError as e:
        if e.conflict or not _is_pat_write_denied(e):
            return json.dumps(
                {
                    "ok": False,
                    "conflict": e.conflict,
                    "error": e.message,
                    "status": e.status,
                },
                ensure_ascii=False,
            )
        # PAT 403 → Deploy Key 推送（建分支+写文件一步完成）
        try:
            pushed = await git_ssh_writer.push_file_via_ssh(
                owner=owner,
                repo=name,
                path=path,
                content=content,
                message=message,
                base_branch="main",
                target_branch=branch,
                read_token=get_settings().github_token,
            )
            return json.dumps(
                {
                    "ok": True,
                    "path": path,
                    "branch": branch,
                    "commit_sha": pushed.get("commit_sha"),
                    "html_url": pushed.get("html_url"),
                    "via": "deploy_key",
                    "detail": pushed.get("detail"),
                    "pr_url": pushed.get("pr_url"),
                },
                ensure_ascii=False,
            )
        except GitSSHError as ssh_e:
            return json.dumps(
                {
                    "ok": False,
                    "error": ssh_e.message,
                    "detail": ssh_e.detail,
                    "status": 403,
                    "hint": "Deploy Key 需勾选 Allow write access",
                },
                ensure_ascii=False,
            )


@tool("create_pr", args_schema=CreatePRInput)
async def create_pr(
    repo: str,
    title: str,
    body: str = "",
    head: str = "",
    base: str = "main",
) -> str:
    """创建 Pull Request。高危操作，图中会先 interrupt 人工审批；带幂等锁防重复 PR。"""
    owner, name = _split_repo(repo)
    idem_key = build_idempotency_key("create_pr", repo=repo, head=head, title=title, base=base)
    lock = await redis_service.acquire_idempotency(idem_key, ttl=600)
    if not lock.acquired:
        return json.dumps(
            {
                "ok": True,
                "deduplicated": True,
                "cached": lock.cached_value,
                "message": "相同 PR 操作已执行，返回缓存结果（幂等）",
            },
            ensure_ascii=False,
        )
    try:
        data = await github_backend().create_pr(owner, name, title, body, head, base=base)
        result = {
            "ok": True,
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "title": data.get("title"),
            "head": head,
            "base": base,
        }
        await redis_service.set_idempotency_result(idem_key, json.dumps(result), ttl=600)
        return json.dumps(result, ensure_ascii=False)
    except GitHubAPIError as e:
        await redis_service.release_idempotency(idem_key)
        return json.dumps({"ok": False, "error": e.message, "status": e.status})


@tool("list_pull_requests", args_schema=ListPRsInput)
async def list_pull_requests(repo: str, state: str = "open", per_page: int = 10) -> str:
    """列出仓库 Pull Requests。查看待合并变更、选 PR 做 review 时使用。"""
    owner, name = _split_repo(repo)
    data = await github_backend().list_pulls(owner, name, state=state, per_page=per_page)
    simplified = [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "user": (p.get("user") or {}).get("login"),
            "head": (p.get("head") or {}).get("ref"),
            "base": (p.get("base") or {}).get("ref"),
            "html_url": p.get("html_url"),
            "draft": p.get("draft"),
        }
        for p in data
    ]
    return json.dumps(simplified, ensure_ascii=False, indent=2)


@tool("list_commits", args_schema=ListCommitsInput)
async def list_commits(repo: str, sha: str = "main", per_page: int = 10, path: str = "") -> str:
    """列出分支近期 commits；可按 path 过滤某文件历史。"""
    owner, name = _split_repo(repo)
    data = await github_backend().list_commits(owner, name, sha=sha, per_page=per_page, path=path)
    simplified = [
        {
            "sha": (c.get("sha") or "")[:10],
            "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0][:200],
            "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
            "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
            "html_url": c.get("html_url"),
        }
        for c in data
    ]
    return json.dumps(simplified, ensure_ascii=False, indent=2)


@tool("create_issue", args_schema=CreateIssueInput)
async def create_issue(repo: str, title: str, body: str = "", labels: Optional[List[str]] = None) -> str:
    """创建 GitHub Issue。适合把审查发现的问题登记为可跟踪任务。"""
    owner, name = _split_repo(repo)
    try:
        data = await github_backend().create_issue(owner, name, title, body=body, labels=labels or [])
        return json.dumps(
            {
                "ok": True,
                "number": data.get("number"),
                "html_url": data.get("html_url"),
                "title": data.get("title"),
            },
            ensure_ascii=False,
        )
    except GitHubAPIError as e:
        return json.dumps({"ok": False, "error": e.message, "status": e.status})


@tool("compare_branches", args_schema=CompareBranchesInput)
async def compare_branches(repo: str, base: str, head: str) -> str:
    """比较两个分支/提交的差异摘要（ahead/behind、变更文件列表）。提 PR 前或审查特性分支时使用。"""
    owner, name = _split_repo(repo)
    try:
        data = await github_backend().compare(owner, name, base, head)
        files = [
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
            }
            for f in (data.get("files") or [])[:40]
        ]
        return json.dumps(
            {
                "ok": True,
                "status": data.get("status"),
                "ahead_by": data.get("ahead_by"),
                "behind_by": data.get("behind_by"),
                "total_commits": data.get("total_commits"),
                "permalink_url": data.get("permalink_url"),
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
        )
    except GitHubAPIError as e:
        return json.dumps({"ok": False, "error": e.message, "status": e.status})


@tool("add_comment", args_schema=AddCommentInput)
async def add_comment(repo: str, number: int, body: str) -> str:
    """在 Issue 或 PR 下添加评论。"""
    owner, name = _split_repo(repo)
    data = await github_backend().add_comment(owner, name, number, body)
    return json.dumps(
        {"ok": True, "id": data.get("id"), "html_url": data.get("html_url")},
        ensure_ascii=False,
    )


@tool("merge_pr", args_schema=MergePRInput)
async def merge_pr(repo: str, number: int, merge_method: str = "squash") -> str:
    """合并 Pull Request。极高危，必须经 interrupt 人工审批后才会真正执行。"""
    owner, name = _split_repo(repo)
    idem_key = build_idempotency_key("merge_pr", repo=repo, number=number)
    lock = await redis_service.acquire_idempotency(idem_key, ttl=600)
    if not lock.acquired:
        return json.dumps({"ok": True, "deduplicated": True, "cached": lock.cached_value})
    try:
        data = await github_backend().merge_pr(owner, name, number, merge_method=merge_method)
        result = {"ok": True, "merged": data.get("merged"), "sha": data.get("sha"), "message": data.get("message")}
        await redis_service.set_idempotency_result(idem_key, json.dumps(result), ttl=600)
        return json.dumps(result, ensure_ascii=False)
    except GitHubAPIError as e:
        await redis_service.release_idempotency(idem_key)
        return json.dumps({"ok": False, "error": e.message, "status": e.status})


ALL_GITHUB_TOOLS = [
    list_issues,
    get_issue,
    list_pull_requests,
    list_commits,
    search_code,
    get_file_content,
    compare_branches,
    create_branch,
    update_file,
    create_issue,
    create_pr,
    add_comment,
    merge_pr,
]
