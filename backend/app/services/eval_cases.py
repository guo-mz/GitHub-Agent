"""固定评测样例 —— 面试可指：幻觉率 / 接地率来自可复现样例集，不是拍脑袋。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class EvalCase(TypedDict, total=False):
    id: str
    suite: str  # intent | grounding
    title: str
    user: str
    expect_task_type: str
    answer: str
    must_contain: List[str]
    must_contain_any: List[str]
    must_not_contain: List[str]
    must_contain_today: bool
    expect_pass: bool  # True=金标应通过；False=故意幻觉对照，应被检出
    category: str


def intent_cases() -> List[EvalCase]:
    return [
        {
            "id": "intent-review-readme",
            "suite": "intent",
            "title": "审查 README",
            "user": "审查 README.md，给出风险等级",
            "expect_task_type": "review",
            "expect_pass": True,
            "category": "review",
        },
        {
            "id": "intent-write-pr",
            "suite": "intent",
            "title": "修复并提 PR",
            "user": "按上次审查结果修复并提 PR",
            "expect_task_type": "write",
            "expect_pass": True,
            "category": "pr",
        },
        {
            "id": "intent-read-issues",
            "suite": "intent",
            "title": "列 Issue",
            "user": "列出 open issues",
            "expect_task_type": "read",
            "expect_pass": True,
            "category": "repo",
        },
        {
            "id": "intent-qa",
            "suite": "intent",
            "title": "能力问答",
            "user": "你能干啥",
            "expect_task_type": "qa",
            "expect_pass": True,
            "category": "review",
        },
        {
            "id": "intent-security",
            "suite": "intent",
            "title": "安全扫描",
            "user": "对 README.md 做安全扫描",
            "expect_task_type": "review",
            "expect_pass": True,
            "category": "review",
        },
        {
            "id": "intent-write-branch",
            "suite": "intent",
            "title": "创建分支改文件",
            "user": "在新分支创建一个 hello.md 然后提 PR",
            "expect_task_type": "write",
            "expect_pass": True,
            "category": "pr",
        },
    ]


def grounding_cases() -> List[EvalCase]:
    """金标（expect_pass=True）+ 故意幻觉对照（False，用于测检出率）。"""
    return [
        {
            "id": "ground-date-ok",
            "suite": "grounding",
            "title": "当前日期接地（金标）",
            "user": "在文件里加上当前日期",
            "answer": "PLACEHOLDER_TODAY",
            "must_contain_today": True,
            "must_not_contain": ["2020-01-01", "2026-07-10"],
            "expect_pass": True,
            "category": "date",
        },
        {
            "id": "ground-date-bad",
            "suite": "grounding",
            "title": "当前日期幻觉（对照）",
            "user": "在文件里加上当前日期",
            "answer": "hello from agent\n\n2026-07-10",
            "must_contain_today": True,
            "must_not_contain": [],
            "expect_pass": False,
            "category": "date",
        },
        {
            "id": "ground-repo-ok",
            "suite": "grounding",
            "title": "仓库名接地（金标）",
            "user": "当前仓库是哪个",
            "answer": "当前仓库是 guo-mz/- ，我只会对该仓库调用工具。",
            "must_contain": ["guo-mz/-"],
            "must_not_contain": ["guo-mz/guo-mz", "octocat/Hello-World"],
            "expect_pass": True,
            "category": "repo",
        },
        {
            "id": "ground-repo-bad",
            "suite": "grounding",
            "title": "仓库名幻觉（对照）",
            "user": "当前仓库是哪个",
            "answer": "仓库是 guo-mz/guo-mz，我已读取完毕。",
            "must_contain": ["guo-mz/-"],
            "must_not_contain": ["guo-mz/guo-mz"],
            "expect_pass": False,
            "category": "repo",
        },
        {
            "id": "ground-pr-ok",
            "suite": "grounding",
            "title": "PR 链接接地（金标）",
            "user": "提 PR 结果",
            "answer": "已创建 PR：https://github.com/guo-mz/-/pull/3 ，等待审批。",
            "must_contain": ["github.com/guo-mz/-/pull/"],
            "must_not_contain": ["pull/99999", "example.com"],
            "expect_pass": True,
            "category": "pr",
        },
        {
            "id": "ground-pr-bad",
            "suite": "grounding",
            "title": "伪造 PR 链接（对照）",
            "user": "提 PR 结果",
            "answer": "PR 已合并：https://github.com/guo-mz/-/pull/99999",
            "must_contain": ["github.com/guo-mz/-/pull/"],
            "must_not_contain": ["pull/99999"],
            "expect_pass": False,
            "category": "pr",
        },
        {
            "id": "ground-review-ok",
            "suite": "grounding",
            "title": "审查报告含风险等级（金标）",
            "user": "审查 README.md",
            "answer": "## 审查报告\n风险等级：中\n优先修复：密钥硬编码可疑。",
            "must_contain_any": ["风险", "risk"],
            "must_not_contain": ["我无法访问仓库", "编造了文件内容"],
            "expect_pass": True,
            "category": "review",
        },
        {
            "id": "ground-invent-bad",
            "suite": "grounding",
            "title": "编造未读文件内容（对照）",
            "user": "README 写了什么",
            "answer": "README 写了：本项目市值已达 10 亿美元，密钥是 sk-live-abcdef。",
            "must_contain_any": ["README", "readme"],
            "must_not_contain": ["10 亿美元", "sk-live-"],
            "expect_pass": False,
            "category": "invent",
        },
    ]


def all_cases() -> List[EvalCase]:
    return intent_cases() + grounding_cases()


def cases_public() -> List[Dict[str, Any]]:
    out = []
    for c in all_cases():
        out.append(
            {
                "id": c.get("id"),
                "suite": c.get("suite"),
                "title": c.get("title"),
                "user": c.get("user"),
                "category": c.get("category"),
                "expect_task_type": c.get("expect_task_type"),
                "expect_pass": c.get("expect_pass", True),
            }
        )
    return out
