"""把挂起工具参数格式化成审批可读摘要（面试演示高潮）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _preview(text: str, limit: int = 600) -> str:
    text = (text or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断）"


def format_pending_diff(
    pending: List[Dict[str, Any]],
    changed_files: Optional[List[str]] = None,
    repo: str = "",
) -> str:
    lines: List[str] = []
    if repo:
        lines.append(f"仓库: {repo}")
    if changed_files:
        uniq = [p for p in dict.fromkeys(changed_files) if p]
        if uniq:
            lines.append("已改文件: " + ", ".join(uniq))
    if lines:
        lines.append("")

    for i, tc in enumerate(pending or [], 1):
        name = tc.get("name") or "unknown"
        args = tc.get("args") or {}
        lines.append(f"{i}. 操作: {name}")

        if name == "create_pr":
            lines.append(f"   标题: {args.get('title') or '（无）'}")
            lines.append(
                f"   分支: {args.get('head') or '?'} → {args.get('base') or 'main'}"
            )
            body = (args.get("body") or "").strip()
            if body:
                lines.append("   说明:")
                for bl in _preview(body, 400).splitlines():
                    lines.append(f"     {bl}")
        elif name == "merge_pr":
            lines.append(f"   PR #{args.get('number') or args.get('pull_number') or '?'}")
            lines.append(f"   方式: {args.get('merge_method') or 'squash'}")
        elif name == "update_file":
            lines.append(f"   文件: {args.get('path') or '?'}")
            lines.append(f"   分支: {args.get('branch') or '?'}")
            msg = args.get("message") or args.get("commit_message")
            if msg:
                lines.append(f"   commit: {msg}")
            content = args.get("content")
            if isinstance(content, str) and content.strip():
                lines.append("   内容预览:")
                for bl in _preview(content).splitlines():
                    lines.append(f"     {bl}")
        elif name == "create_branch":
            lines.append(f"   新分支: {args.get('branch') or args.get('name') or '?'}")
            lines.append(f"   来源: {args.get('from_branch') or args.get('base') or 'main'}")
        else:
            for k, v in list(args.items())[:8]:
                sv = str(v)
                if len(sv) > 120:
                    sv = sv[:120] + "…"
                lines.append(f"   {k}: {sv}")
        lines.append("")

    if not pending:
        lines.append("（无待审批工具调用）")
    return "\n".join(lines).strip()
