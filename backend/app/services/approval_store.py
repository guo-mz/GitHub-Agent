"""审批 pending/resolved 本地持久化 —— 无 Redis 时跨进程重启仍可拦截旧 checkpoint resume。

与 redis_service 双写：内存/Redis 作热路径，.data/approvals.json 作真相源补充。
"""

from __future__ import annotations

import json
import os
import threading
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STORE_PATH = Path(__file__).resolve().parents[2] / ".data" / "approvals.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> Dict[str, Any]:
    if not STORE_PATH.exists():
        return {"pending": {}, "resolved": {}}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"pending": {}, "resolved": {}}
        data.setdefault("pending", {})
        data.setdefault("resolved", {})
        return data
    except Exception:  # noqa: BLE001
        return {"pending": {}, "resolved": {}}


def _save(data: Dict[str, Any]) -> None:
    """原子写入：先写临时文件再 replace，降低半写损坏风险（仍非跨进程事务）。"""
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(STORE_PATH.parent), prefix=".approvals.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_pending(tid: str, payload: Dict[str, Any]) -> None:
    with _LOCK:
        data = _load()
        row = dict(payload)
        row.setdefault("thread_id", tid)
        row.setdefault("status", "pending")
        row["updated_at"] = _now()
        data["pending"][tid] = row
        # 重新挂起时清掉旧 resolved（同 tid 新一轮审批）
        data["resolved"].pop(tid, None)
        _save(data)


def get_pending(tid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        row = data["pending"].get(tid)
        return dict(row) if row else None


def mark_resolved(
    tid: str,
    *,
    decision: str = "",
    approval_kind: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    with _LOCK:
        data = _load()
        pending = data["pending"].pop(tid, None) or {}
        kind = approval_kind or pending.get("approval_kind") or ""
        resolved = {
            "thread_id": tid,
            "status": "resolved",
            "decision": decision or "resolved",
            "approval_kind": kind,
            "resolved_at": _now(),
            "task_id": pending.get("task_id") or tid,
            "phase": pending.get("phase") or "",
        }
        if extra:
            resolved.update(extra)
        data["resolved"][tid] = resolved
        _save(data)
        return dict(resolved)


def get_resolved(tid: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _load()
        row = data["resolved"].get(tid)
        return dict(row) if row else None


def clear_all_for_tests() -> None:
    with _LOCK:
        _save({"pending": {}, "resolved": {}})
