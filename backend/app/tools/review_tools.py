"""代码审查 / 安全扫描 / 修复建议工具。

对齐 Hello-Agents CodeReviewAgent，并扩展为「审查 → 记忆 → 建议修复 → 写分支提 PR」闭环。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.services.fix_service import suggest_fixed_content
from app.services.github_client import GitHubAPIError
from app.services.github_rpc import github_backend
from app.services.redis_service import redis_service
from app.services.review_service import review_service
from app.services.security_scan import findings_to_dict, scan_text


def _split_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2:
        raise ValueError("repo 必须是 owner/name 格式")
    return parts[0], parts[1]


class ReviewFileInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    path: str = Field(..., description="要审查的文件路径，如 src/auth.py")
    ref: str = Field(default="main", description="分支名或 commit SHA")
    focus: str = Field(default="", description="可选审查重点，如「安全」「并发」")


class ReviewPRInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    number: int = Field(..., description="Pull Request 编号")
    focus: str = Field(default="", description="可选审查重点")


class SecurityScanInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    path: str = Field(..., description="文件路径")
    ref: str = Field(default="main", description="分支或 SHA")


class SuggestFixInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    path: str = Field(..., description="要修复的文件路径")
    ref: str = Field(default="main", description="读取原文件的分支/SHA")
    issues: str = Field(
        default="",
        description="要修复的问题描述；为空则自动读取该仓库最近一次审查记忆",
    )


class GetLastReviewInput(BaseModel):
    repo: str = Field(..., description="owner/repo")


@tool("review_file", args_schema=ReviewFileInput)
async def review_file(repo: str, path: str, ref: str = "main", focus: str = "") -> str:
    """对仓库单个文件做智能代码审查（静态启发式 + LLM）：质量/bug/安全/性能。
    结果会缓存为审查记忆。用户说「审查某文件 / code review / 找 bug」时优先使用。只读。"""
    owner, name = _split_repo(repo)
    data = await review_service.review_file(owner, name, path, ref=ref, focus=focus)
    return json.dumps(data, ensure_ascii=False, indent=2)


@tool("review_pr", args_schema=ReviewPRInput)
async def review_pr(repo: str, number: int, focus: str = "") -> str:
    """对 PR 变更做智能代码审查（静态扫描 patch + LLM 报告）。只读，结果写入审查记忆。"""
    owner, name = _split_repo(repo)
    data = await review_service.review_pr(owner, name, number, focus=focus)
    return json.dumps(data, ensure_ascii=False, indent=2)


@tool("security_scan_file", args_schema=SecurityScanInput)
async def security_scan_file(repo: str, path: str, ref: str = "main") -> str:
    """对文件做快速静态安全/质量扫描（密钥、注入、eval、裸 except 等），不调用 LLM，速度快。
    用户强调「安全扫描 / 查密钥泄露」时使用。"""
    owner, name = _split_repo(repo)
    try:
        data = await github_backend().get_file_content(owner, name, path, ref=ref)
    except GitHubAPIError as e:
        return json.dumps({"ok": False, "error": e.message, "status": e.status})
    if data.get("type") == "dir":
        return json.dumps({"ok": False, "error": "请指定文件而非目录"})
    content = data.get("content") or ""
    findings = scan_text(content, path)
    out = findings_to_dict(findings)
    out.update({"ok": True, "repo": repo, "path": path, "ref": ref, "sha": data.get("sha")})
    return json.dumps(out, ensure_ascii=False, indent=2)


@tool("get_last_review", args_schema=GetLastReviewInput)
async def get_last_review(repo: str) -> str:
    """读取该仓库最近一次审查记忆（report 摘要）。用户说「按上次审查结果修复」时先调用。"""
    mem = await redis_service.get_review_memory(repo)
    if not mem:
        return json.dumps({"ok": False, "error": "没有缓存的审查结果，请先 review_file 或 review_pr"})
    return json.dumps({"ok": True, "memory": mem}, ensure_ascii=False, indent=2)


@tool("suggest_fix", args_schema=SuggestFixInput)
async def suggest_fix(repo: str, path: str, ref: str = "main", issues: str = "") -> str:
    """根据审查问题生成修复后的完整文件内容（不写仓库）。
    返回 suggested_content 与当前 sha；随后应 create_branch → update_file(用建议内容) → create_pr。
    issues 为空时自动使用 get_last_review 记忆。"""
    owner, name = _split_repo(repo)
    issue_text = issues.strip()
    if not issue_text:
        mem = await redis_service.get_review_memory(repo)
        if not mem:
            return json.dumps(
                {"ok": False, "error": "无审查记忆且未提供 issues，请先 review_file 或传入 issues"},
                ensure_ascii=False,
            )
        issue_text = mem.get("report") or json.dumps(mem.get("static_scan") or {}, ensure_ascii=False)
    try:
        data = await github_backend().get_file_content(owner, name, path, ref=ref)
    except GitHubAPIError as e:
        return json.dumps({"ok": False, "error": e.message, "status": e.status})
    if data.get("type") == "dir":
        return json.dumps({"ok": False, "error": "请指定文件"})
    content = data.get("content") or ""
    fixed = await suggest_fixed_content(path=path, content=content, issues=issue_text)
    return json.dumps(
        {
            "ok": True,
            "repo": repo,
            "path": path,
            "ref": ref,
            "sha": data.get("sha"),
            "suggested_content": fixed,
            "hint": "下一步：create_branch → update_file(content=suggested_content, sha=上面的sha) → create_pr",
        },
        ensure_ascii=False,
        indent=2,
    )


ALL_REVIEW_TOOLS = [
    review_file,
    review_pr,
    security_scan_file,
    get_last_review,
    suggest_fix,
]




