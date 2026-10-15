"""统一 GitHub 远程写入门禁。

只有显式 permit（当前仅 Publish Agent 在通过 plan/diff/publish/test/hash 校验后）
才能调用 github_client / git_ssh 写方法。编辑器、ReAct 工具、bootstrap、裸 API 默认拒绝。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional

from app.services import audit_service


class GitHubWriteDenied(PermissionError):
    """未持有写入许可时抛出。"""


@dataclass(frozen=True)
class WritePermit:
    task_id: str
    reason: str
    content_hash: str
    repo: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


_PERMIT: contextvars.ContextVar[Optional[WritePermit]] = contextvars.ContextVar(
    "github_write_permit", default=None
)


def current_permit() -> Optional[WritePermit]:
    return _PERMIT.get()


@contextmanager
def permit_github_write(
    *,
    task_id: str,
    reason: str,
    content_hash: str,
    repo: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Iterator[WritePermit]:
    permit = WritePermit(
        task_id=task_id or "",
        reason=reason,
        content_hash=content_hash or "",
        repo=repo or "",
        extra=dict(extra or {}),
    )
    token = _PERMIT.set(permit)
    try:
        yield permit
    finally:
        _PERMIT.reset(token)


def require_github_write(*, operation: str, repo: str = "") -> WritePermit:
    permit = _PERMIT.get()
    if permit is None:
        audit_service.record_security_event(
            action="github_write",
            result="denied",
            reason="no_write_permit",
            operation=operation,
            repo=repo,
        )
        raise GitHubWriteDenied(
            f"GitHub 写操作被拒绝：{operation} 需要 Publish Agent 写入许可"
        )
    if repo and permit.repo and repo != permit.repo:
        audit_service.record_security_event(
            action="github_write",
            result="denied",
            reason="repo_mismatch",
            operation=operation,
            repo=repo,
            task_id=permit.task_id,
        )
        raise GitHubWriteDenied(
            f"GitHub 写操作被拒绝：repo 与许可不符 ({repo} vs {permit.repo})"
        )
    audit_service.record_security_event(
        action="github_write",
        result="allowed",
        reason=permit.reason,
        operation=operation,
        repo=repo or permit.repo,
        task_id=permit.task_id,
        content_hash=permit.content_hash[:16] if permit.content_hash else "",
    )
    return permit
