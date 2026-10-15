"""本地审计日志：提交历史 + 审批记录（无 Redis/DB 也能持久化，便于回滚）。

存 backend/.data/audit_log.json，重启不丢。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

STORE_PATH = Path(__file__).resolve().parents[2] / ".data" / "audit_log.json"
_LOCK = threading.Lock()
_MAX = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> List[Dict[str, Any]]:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(items: List[Dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(items[:_MAX], ensure_ascii=False, indent=2), encoding="utf-8")


def _rollback_links(
    *,
    owner: str,
    repo: str,
    base: str,
    branch: str,
    commit_sha: str = "",
    pr_number: Optional[int] = None,
) -> Dict[str, str]:
    base_url = f"https://github.com/{owner}/{repo}"
    out = {
        "branch_url": f"{base_url}/tree/{branch}" if branch else base_url,
        "compare_url": f"{base_url}/compare/{base}...{branch}" if branch else "",
        "commits_url": f"{base_url}/commits/{branch}" if branch else f"{base_url}/commits/{base}",
        "hint": "回滚建议：未合并用关闭 PR / 删分支；已合并在 PR 页点 Revert，或 checkout 旧 commit。",
    }
    if commit_sha:
        out["commit_url"] = f"{base_url}/commit/{commit_sha}"
    if pr_number:
        out["pr_url"] = f"{base_url}/pull/{pr_number}"
        out["hint"] = (
            f"回滚：打开 PR #{pr_number} → 已合并则点 Revert；"
            f"未合并则 Close PR 并删除分支 {branch}。"
        )
    return out


def add_event(event: Dict[str, Any]) -> Dict[str, Any]:
    item = {
        "id": event.get("id") or uuid.uuid4().hex[:12],
        "ts": event.get("ts") or _now(),
        **{k: v for k, v in event.items() if k not in {"id", "ts"}},
    }
    with _LOCK:
        items = _load()
        items.insert(0, item)
        _save(items)
    return item


def list_events(
    *,
    kind: str = "",
    repo: str = "",
    limit: int = 80,
) -> List[Dict[str, Any]]:
    with _LOCK:
        items = _load()
    out = []
    for it in items:
        if kind and it.get("kind") != kind:
            continue
        if repo and it.get("repo") != repo:
            continue
        out.append(it)
        if len(out) >= limit:
            break
    return out


def update_event(event_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    with _LOCK:
        items = _load()
        for i, it in enumerate(items):
            if it.get("id") == event_id or it.get("thread_id") == event_id:
                items[i] = {**it, **patch, "updated_at": _now()}
                _save(items)
                return items[i]
    return None


def record_save(
    *,
    owner: str,
    repo: str,
    path: str,
    branch: str,
    base_branch: str,
    commit_sha: str = "",
    pr_url: str = "",
    pr_number: Optional[int] = None,
    message: str = "",
    source: str = "editor",
    detail: str = "",
) -> Dict[str, Any]:
    full = f"{owner}/{repo}"
    return add_event(
        {
            "kind": "commit",
            "source": source,
            "repo": full,
            "path": path,
            "branch": branch,
            "base_branch": base_branch,
            "commit_sha": commit_sha,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "message": message,
            "status": "success",
            "summary": detail or f"保存 {path} → {branch}",
            "rollback": _rollback_links(
                owner=owner,
                repo=repo,
                base=base_branch,
                branch=branch,
                commit_sha=commit_sha,
                pr_number=pr_number,
            ),
        }
    )


def record_approval_pending(
    *,
    thread_id: str,
    repo: str,
    operation: str,
    risk_level: str,
    summary: str,
    diff: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    owner, name = (repo.split("/") + ["", ""])[:2]
    args = (payload or {}).get("tool_args") or {}
    head = args.get("head") or args.get("branch") or ""
    base = args.get("base") or "main"
    row = {
        "kind": "approval",
        "thread_id": thread_id,
        "repo": repo,
        "operation": operation,
        "risk_level": risk_level,
        "summary": summary,
        "diff": diff,
        "status": "pending",
        "branch": head,
        "base_branch": base,
        "payload": payload or {},
        "rollback": _rollback_links(owner=owner, repo=name, base=base, branch=head)
        if owner and name
        else {},
    }
    # 同一 thread 已有 pending 则更新，避免重复刷屏
    with _LOCK:
        items = _load()
        for i, it in enumerate(items):
            if (
                it.get("kind") == "approval"
                and it.get("thread_id") == thread_id
                and it.get("status") == "pending"
            ):
                items[i] = {**it, **row, "id": it.get("id"), "ts": it.get("ts") or _now()}
                _save(items)
                return items[i]
    return add_event(row)


def record_security_event(
    *,
    action: str,
    result: str,
    reason: str = "",
    operation: str = "",
    repo: str = "",
    task_id: str = "",
    thread_id: str = "",
    content_hash: str = "",
    approval_kind: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """结构化安全审计：写入允许/拒绝、哈希、测试门禁、降级等。禁止写入秘密。"""
    row: Dict[str, Any] = {
        "kind": "security",
        "action": action,
        "result": result,
        "reason": (reason or "")[:500],
        "operation": operation,
        "repo": repo,
        "task_id": task_id,
        "thread_id": thread_id or task_id,
        "content_hash": (content_hash or "")[:64],
        "approval_kind": approval_kind,
        "summary": f"{action}:{result}:{reason}"[:240],
    }
    if extra:
        # 过滤可能敏感字段
        safe = {
            k: v
            for k, v in extra.items()
            if k.lower() not in {"token", "password", "secret", "authorization", "api_key"}
        }
        row["extra"] = safe
    return add_event(row)


def record_approval_decision(
    *,
    thread_id: str,
    decision: str,
    comment: str = "",
) -> Optional[Dict[str, Any]]:
    status = "approved" if decision == "approve" else "rejected"
    # 更新最近一条同 thread 的 pending；若没有则新建
    with _LOCK:
        items = _load()
        for i, it in enumerate(items):
            if it.get("kind") == "approval" and it.get("thread_id") == thread_id:
                items[i] = {
                    **it,
                    "status": status,
                    "decision": decision,
                    "comment": comment,
                    "decided_at": _now(),
                }
                _save(items)
                return items[i]
    return add_event(
        {
            "kind": "approval",
            "thread_id": thread_id,
            "status": status,
            "decision": decision,
            "comment": comment,
            "summary": f"审批{status}",
            "operation": "approval",
        }
    )


audit_service = type("AuditService", (), {})()  # namespace marker
