"""六层护栏 —— 高风险写操作的最后一道闸门。

面试点：护栏要在 ToolNode 之后、真正副作用之前执行；
interrupt 审批属于第 4 层，与技术硬拦截（分支/路径）分层，避免 LLM 绕过。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config import get_settings

BLOCKED_BRANCHES: Set[str] = {"main", "master", "release", "production"}
BLOCKED_PATHS: Set[str] = {".github/workflows", "Dockerfile", ".env", "secrets/"}
HIGH_RISK_TOOLS: Set[str] = {"create_pr", "merge_pr", "update_file"}
ALWAYS_APPROVE_TOOLS: Set[str] = {"create_pr", "merge_pr"}


class PathSecurityError(ValueError):
    """写入/发布路径未通过护栏。code 可诊断；勿附带密钥或主机绝对路径。"""

    def __init__(self, code: str, path: str = ""):
        self.code = code
        self.path = path
        detail = f"{code}:{path}" if path else code
        super().__init__(detail)


@dataclass
class GuardrailResult:
    passed: bool
    need_approval: bool = False
    risk_level: str = "low"
    reasons: List[str] = field(default_factory=list)
    blocked: bool = False
    approval_payload: Dict[str, Any] = field(default_factory=dict)


def _normalize_path(path: str) -> str:
    # 注意：不能用 str.lstrip("./")——那是按字符集剥离，会把 ".env" 变成 "env"
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _structural_path_issue(raw: str) -> Optional[Tuple[str, str]]:
    """结构层：绝对路径 / 穿越 / 空路径。返回 (code, display_path) 或 None。"""
    if raw is None or not str(raw).strip():
        return "empty_path", ""
    text = str(raw).strip()
    if "\x00" in text:
        return "invalid_path", ""
    # Windows 盘符或 UNC / POSIX 绝对路径
    if text.startswith("/") or text.startswith("\\"):
        return "absolute_path_forbidden", _normalize_path(text)[:120]
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return "absolute_path_forbidden", text[:120]
    normalized = _normalize_path(text)
    if not normalized:
        return "empty_path", ""
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or ".." in normalized.split("/"):
        return "path_traversal_forbidden", normalized[:120]
    # 折叠 ./ 后仍含穿越语义
    collapsed: List[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return "path_traversal_forbidden", normalized[:120]
        collapsed.append(part)
    if not collapsed:
        return "empty_path", ""
    return None


def _path_blocked(path: str) -> bool:
    issue = _structural_path_issue(path)
    if issue is not None:
        return True
    normalized = _normalize_path(path)
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    for blocked in BLOCKED_PATHS:
        if blocked.endswith("/"):
            prefix = blocked.rstrip("/")
            if normalized == prefix or normalized.startswith(blocked) or normalized.startswith(prefix + "/"):
                return True
            continue
        if blocked == ".github/workflows":
            if normalized == blocked or normalized.startswith(".github/workflows/"):
                return True
            continue
        if blocked in {".env", "Dockerfile"}:
            if normalized == blocked or normalized.endswith(f"/{blocked}") or blocked in parts:
                return True
            continue
        if normalized == blocked or normalized.endswith(f"/{blocked}") or blocked in parts:
            return True
    return False


def validate_write_path(path: str) -> str:
    """规范化并校验可写相对路径；非法/敏感路径抛 PathSecurityError。"""
    issue = _structural_path_issue(path)
    if issue is not None:
        code, display = issue
        raise PathSecurityError(code, display)
    normalized = _normalize_path(str(path).strip())
    # 再跑黑名单（与 check_path_whitelist 同源规则）
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    for blocked in BLOCKED_PATHS:
        if blocked.endswith("/"):
            prefix = blocked.rstrip("/")
            if (
                normalized == prefix
                or normalized.startswith(blocked)
                or normalized.startswith(prefix + "/")
            ):
                raise PathSecurityError("sensitive_path_forbidden", normalized[:120])
            continue
        if blocked == ".github/workflows":
            if normalized == blocked or normalized.startswith(".github/workflows/"):
                raise PathSecurityError("sensitive_path_forbidden", normalized[:120])
            continue
        if blocked in {".env", "Dockerfile"}:
            if (
                normalized == blocked
                or normalized.endswith(f"/{blocked}")
                or blocked in parts
            ):
                raise PathSecurityError("sensitive_path_forbidden", normalized[:120])
            continue
        if (
            normalized == blocked
            or normalized.endswith(f"/{blocked}")
            or blocked in parts
        ):
            raise PathSecurityError("sensitive_path_forbidden", normalized[:120])
    return normalized


def validate_write_paths(paths: List[str]) -> Optional[str]:
    """批量校验；失败返回可诊断短错误，成功返回 None。"""
    for p in paths or []:
        try:
            validate_write_path(str(p))
        except PathSecurityError as exc:
            return str(exc)
    return None


def check_branch_isolation(branch: Optional[str]) -> Optional[str]:
    """层1：分支隔离 —— 禁止写受保护分支。"""
    if not branch:
        return None
    base = branch.split("/")[-1].lower()
    if branch.lower() in BLOCKED_BRANCHES or base in BLOCKED_BRANCHES:
        return f"禁止写入受保护分支: {branch}"
    return None


def check_path_whitelist(paths: List[str]) -> Optional[str]:
    """层2：路径白名单（黑名单实现）—— 禁止改敏感文件。"""
    # 结构非法也拦截（与 validate_write_path 对齐）
    for p in paths or []:
        issue = _structural_path_issue(str(p))
        if issue is not None:
            code, display = issue
            label = display or code
            return f"禁止修改敏感路径: {label}"
    blocked = [p for p in paths if _path_blocked(p)]
    if blocked:
        return f"禁止修改敏感路径: {', '.join(blocked)}"
    return None


def check_change_volume(file_count: int, line_count: int) -> Optional[str]:
    """层3：变更量限制 —— 超阈值强制人工。"""
    settings = get_settings()
    if file_count > settings.max_files_changed:
        return f"变更文件数 {file_count} 超过阈值 {settings.max_files_changed}"
    if line_count > settings.max_lines_changed:
        return f"变更行数 {line_count} 超过阈值 {settings.max_lines_changed}"
    return None


def estimate_line_diff(content: str, previous: str = "") -> int:
    new_lines = content.count("\n") + (1 if content else 0)
    old_lines = previous.count("\n") + (1 if previous else 0)
    return abs(new_lines - old_lines) + min(new_lines, old_lines)  # 粗估


def run_guardrails(
    tool_name: str,
    tool_args: Dict[str, Any],
    changed_files: Optional[List[str]] = None,
    lines_changed: int = 0,
    diff: str = "",
) -> GuardrailResult:
    """综合六层护栏（1-4 在此同步执行；5 幂等 / 6 乐观锁在工具层）。"""
    reasons: List[str] = []
    need_approval = False
    risk = "low"
    blocked = False

    branch = tool_args.get("branch") or tool_args.get("head") or tool_args.get("base")
    path = tool_args.get("path") or tool_args.get("file_path")
    paths = list(changed_files or [])
    if path:
        paths.append(path)

    # 层1 分支
    if tool_name in {"update_file", "create_branch"} or tool_name in HIGH_RISK_TOOLS:
        msg = check_branch_isolation(str(branch) if branch else None)
        # create_pr 的 base 可以是 main，但 head 不能是受保护分支被直接 push
        if tool_name == "update_file":
            msg = check_branch_isolation(str(tool_args.get("branch") or ""))
            if msg:
                reasons.append(msg)
                blocked = True
                risk = "critical"

    # 层2 路径
    if paths:
        msg = check_path_whitelist(paths)
        if msg:
            reasons.append(msg)
            blocked = True
            risk = "critical"

    # 层3 变更量
    file_count = len(set(paths)) if paths else 0
    vol = check_change_volume(file_count or 1 if tool_name == "update_file" else file_count, lines_changed)
    if vol:
        reasons.append(vol)
        need_approval = True
        risk = "high"

    # 层4 人工审批：高危工具强制 interrupt
    if tool_name in ALWAYS_APPROVE_TOOLS:
        need_approval = True
        risk = "critical" if tool_name == "merge_pr" else "high"
        reasons.append(f"高危操作 {tool_name} 必须人工审批")

    if tool_name == "update_file" and not blocked:
        # 写文件默认中风险，可配置；敏感场景已在路径层拦截
        if lines_changed > 50:
            need_approval = True
            risk = "medium"
            reasons.append("单文件变更较大，建议审批")

    passed = not blocked
    payload = {
        "tool_name": tool_name,
        "tool_args": tool_args,
        "reasons": reasons,
        "risk_level": risk,
        "diff": diff,
        "changed_files": paths,
    }
    return GuardrailResult(
        passed=passed,
        need_approval=need_approval and passed,
        risk_level=risk,
        reasons=reasons,
        blocked=blocked,
        approval_payload=payload,
    )
