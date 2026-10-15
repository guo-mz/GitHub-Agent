"""智能代码审查 —— 对齐 Hello-Agents 毕设「CodeReviewAgent」能力。

静态启发式 + LLM 语义审查；结果写入 Redis 审查记忆，供后续修复闭环。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_utils import make_chat_llm
from app.services.github_client import GitHubAPIError
from app.services.github_rpc import github_backend
from app.services.llm_usage.context import llm_call_context
from app.services.llm_usage.recorder import ainvoke_tracked, primary_provider_info
from app.services.redis_service import redis_service
from app.services.security_scan import findings_markdown, findings_to_dict, scan_text

logger = logging.getLogger(__name__)

MAX_CHARS = 36000

REVIEW_SYSTEM = """你是资深代码审查专家（Code Reviewer），服务企业级研发场景。
根据给定代码或 PR diff，以及「静态扫描」结果，输出结构化中文 Markdown 审查报告，禁止编造看不到的代码。

必须包含以下章节（有则写，无则写「未发现明显问题」）：
## 总览
（1～3 句：风险等级 low/medium/high/critical + 结论；综合静态扫描与语义判断）

## 代码质量
（风格、命名、结构、注释、可维护性）

## 潜在 Bug
（逻辑错误、边界条件、空指针/异常、资源泄漏、并发问题；尽量标出行号或片段）

## 安全风险
（注入、密钥泄露、权限、不安全反序列化等；务必回应静态扫描命中项）

## 性能与优化
（复杂度、IO、缓存、不必要分配等）

## 最佳实践建议
（语言/框架惯用法、设计改进，给出可落地建议）

## 优先修复清单
用有序列表，按严重程度排序；每项一行：`[级别] 问题 — 建议`

规则：
- 只基于提供的内容判断，不确定就标注「需人工确认」
- 用简洁条目，避免空话
- 不要输出 JSON，不要输出思考过程
"""


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…(已截断，原长度 {len(text)} 字符)"


async def _run_review(user_payload: str, *, label: str = "review_file") -> str:
    provider, model = primary_provider_info()
    llm = make_chat_llm(temperature=0.1, streaming=False)
    with llm_call_context(module_name="review", node_name=label, label=label):
        resp = await ainvoke_tracked(
            llm,
            [
                SystemMessage(content=REVIEW_SYSTEM),
                HumanMessage(content=user_payload),
            ],
            provider=provider,
            requested_model=model,
            label=label,
            call_type="chat",
        )
    return (resp.content or "").strip() or "（审查结果为空）"


async def review_file_content(
    *,
    repo: str,
    path: str,
    content: str,
    ref: str = "main",
    focus: str = "",
    static_md: str = "",
) -> str:
    lang_hint = path.rsplit(".", 1)[-1] if "." in path else "text"
    focus_line = f"\n审查重点：{focus}\n" if focus.strip() else ""
    payload = (
        f"仓库：{repo}\n文件：{path}\n分支/ref：{ref}\n语言提示：{lang_hint}\n"
        f"{focus_line}\n{static_md}\n```{lang_hint}\n{_clip(content)}\n```"
    )
    return await _run_review(payload, label="review_file")


async def review_pr_payload(
    *,
    repo: str,
    number: int,
    title: str,
    body: str,
    files: List[Dict[str, Any]],
    focus: str = "",
    static_md: str = "",
) -> str:
    parts: List[str] = [
        f"仓库：{repo}",
        f"PR #{number}：{title}",
        f"描述：{(body or '')[:2000]}",
    ]
    if focus.strip():
        parts.append(f"审查重点：{focus}")
    if static_md:
        parts.append(static_md)
    parts.append(f"变更文件数：{len(files)}")
    budget = MAX_CHARS
    for f in files[:20]:
        filename = f.get("filename") or f.get("path") or "unknown"
        status = f.get("status", "")
        patch = f.get("patch") or ""
        additions = f.get("additions")
        deletions = f.get("deletions")
        block = (
            f"\n### {filename} ({status}, +{additions}/-{deletions})\n"
            f"```diff\n{_clip(patch, min(8000, budget))}\n```\n"
        )
        if budget <= 0:
            parts.append("\n…其余文件因长度限制省略")
            break
        parts.append(block)
        budget -= len(block)
    return await _run_review("\n".join(parts), label="review_pr")


class ReviewService:
    async def review_file(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str = "main",
        focus: str = "",
    ) -> Dict[str, Any]:
        full = f"{owner}/{repo}"
        try:
            data = await github_backend().get_file_content(owner, repo, path, ref=ref)
        except GitHubAPIError as e:
            return {"ok": False, "error": e.message, "status": e.status}
        if data.get("type") == "dir":
            return {"ok": False, "error": f"{path} 是目录，请指定文件"}
        content = data.get("content") or ""
        if not content.strip():
            return {"ok": False, "error": "文件内容为空"}

        findings = scan_text(content, path)
        static = findings_to_dict(findings)
        static_md = findings_markdown(findings)
        report = await review_file_content(
            repo=full,
            path=path,
            content=content,
            ref=ref,
            focus=focus,
            static_md=static_md,
        )
        full_report = static_md + "\n" + report
        result = {
            "ok": True,
            "kind": "file",
            "repo": full,
            "path": path,
            "ref": ref,
            "sha": data.get("sha"),
            "html_url": data.get("html_url"),
            "static_scan": static,
            "report": full_report,
        }
        await redis_service.save_review_memory(
            full,
            {
                "kind": "file",
                "path": path,
                "ref": ref,
                "sha": data.get("sha"),
                "risk_level": static.get("risk_level"),
                "report": full_report[:12000],
                "static_scan": static,
            },
        )
        return result

    async def review_pr(
        self,
        owner: str,
        repo: str,
        number: int,
        focus: str = "",
    ) -> Dict[str, Any]:
        full = f"{owner}/{repo}"
        try:
            pr = await github_backend().get_pull(owner, repo, number)
            files = await github_backend().list_pull_files(owner, repo, number)
        except GitHubAPIError as e:
            return {"ok": False, "error": e.message, "status": e.status}

        # 对 patch 做静态扫描（+ 行）
        all_findings = []
        for f in files[:20]:
            patch = f.get("patch") or ""
            added = "\n".join(
                line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")
            )
            all_findings.extend(scan_text(added, f.get("filename") or ""))
        static = findings_to_dict(all_findings)
        static_md = findings_markdown(all_findings)

        report = await review_pr_payload(
            repo=full,
            number=number,
            title=pr.get("title") or "",
            body=pr.get("body") or "",
            files=files,
            focus=focus,
            static_md=static_md,
        )
        full_report = static_md + "\n" + report
        result = {
            "ok": True,
            "kind": "pull_request",
            "repo": full,
            "number": number,
            "title": pr.get("title"),
            "html_url": pr.get("html_url"),
            "head": (pr.get("head") or {}).get("ref"),
            "base": (pr.get("base") or {}).get("ref"),
            "changed_files": pr.get("changed_files"),
            "files_reviewed": [
                {
                    "filename": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                }
                for f in files[:30]
            ],
            "static_scan": static,
            "report": full_report,
        }
        await redis_service.save_review_memory(
            full,
            {
                "kind": "pull_request",
                "number": number,
                "title": pr.get("title"),
                "head": result["head"],
                "risk_level": static.get("risk_level"),
                "report": full_report[:12000],
                "static_scan": static,
            },
        )
        return result


review_service = ReviewService()
