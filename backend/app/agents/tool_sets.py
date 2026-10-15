"""按角色划分工具权限 —— Coding/Publish 写工具在计划确认前禁止调用。"""

from __future__ import annotations

from typing import Any, List, Set

from app.tools import ALL_TOOLS
from app.tools.github_tools import (
    compare_branches,
    get_file_content,
    get_issue,
    list_commits,
    list_issues,
    list_pull_requests,
    search_code,
)
from app.tools.rag_tools import search_code_graph, search_codebase
from app.tools.review_tools import (
    get_last_review,
    review_file,
    review_pr,
    security_scan_file,
)

# 仓库分析：只读
ANALYSIS_TOOLS: List[Any] = [
    get_file_content,
    search_code,
    list_issues,
    list_pull_requests,
    list_commits,
    compare_branches,
    get_issue,
    search_codebase,
    search_code_graph,
    review_file,
    review_pr,
    security_scan_file,
    get_last_review,
]

# 计划确认前禁止的写/发布工具
WRITE_TOOL_NAMES: Set[str] = {
    "create_branch",
    "update_file",
    "create_pr",
    "merge_pr",
    "create_issue",
    "add_comment",
    "suggest_fix",  # 生成修复内容接近编码，计划确认前禁用
}

PUBLISH_TOOL_NAMES: Set[str] = {
    "create_pr",
    "merge_pr",
}


def tools_for_agent(*, plan_confirmed: bool, allow_publish: bool = False) -> List[Any]:
    """通用 ReAct agent 可用工具：未确认计划时剥离写工具。"""
    out = []
    for t in ALL_TOOLS:
        name = getattr(t, "name", "") or ""
        if name in WRITE_TOOL_NAMES and not plan_confirmed:
            continue
        if name in PUBLISH_TOOL_NAMES and not allow_publish:
            continue
        out.append(t)
    return out


def is_write_tool(name: str) -> bool:
    return (name or "") in WRITE_TOOL_NAMES
