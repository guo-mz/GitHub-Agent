"""隔离编码工作区 —— 仅改本地副本，禁止直接写远程 GitHub。

布局：
  backend/.data/workspaces/{task_id}/
    meta.json
    base/   # 从 GitHub 拉下的只读基线
    work/   # Coding Agent 可写工作副本
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2] / ".data" / "workspaces"
_SAFE_PATH = re.compile(r"^(?!\.\.)(?!/)(?!.*\.\.)[A-Za-z0-9_./\-]+$")


@dataclass
class FileDiff:
    path: str
    status: str  # modified|added|deleted
    unified_diff: str
    old_content: str = ""
    new_content: str = ""
    additions: int = 0
    deletions: int = 0


def _task_root(task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", task_id or "unknown")[:80]
    return ROOT / safe


def ensure_workspace(task_id: str, *, repo: str, ref: str = "main") -> Path:
    root = _task_root(task_id)
    (root / "base").mkdir(parents=True, exist_ok=True)
    (root / "work").mkdir(parents=True, exist_ok=True)
    meta = {"task_id": task_id, "repo": repo, "ref": ref, "root": str(root)}
    (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def normalize_rel_path(path: str) -> str:
    p = (path or "").replace("\\", "/").lstrip("/")
    if not p or not _SAFE_PATH.match(p):
        raise ValueError(f"unsafe workspace path: {path!r}")
    return p


def _abs(root: Path, side: str, rel: str) -> Path:
    rel = normalize_rel_path(rel)
    base = (root / side).resolve()
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(f"path escape blocked: {rel}")
    return target


def write_base_file(task_id: str, rel: str, content: str) -> None:
    root = _task_root(task_id)
    path = _abs(root, "base", rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if content is not None else ""
    path.write_text(text, encoding="utf-8")
    w = _abs(root, "work", rel)
    if not w.exists():
        w.parent.mkdir(parents=True, exist_ok=True)
        w.write_text(text, encoding="utf-8")


def read_work_file(task_id: str, rel: str) -> Optional[str]:
    path = _abs(_task_root(task_id), "work", rel)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def read_base_file(task_id: str, rel: str) -> Optional[str]:
    path = _abs(_task_root(task_id), "base", rel)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_work_file(task_id: str, rel: str, content: str) -> None:
    """仅写入 work/，永不触碰 GitHub。"""
    path = _abs(_task_root(task_id), "work", rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else "", encoding="utf-8")


def _count_hunk_stats(diff_text: str) -> Tuple[int, int]:
    add = del_ = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            add += 1
        elif line.startswith("-"):
            del_ += 1
    return add, del_


def compute_file_diff(task_id: str, rel: str) -> Optional[FileDiff]:
    old = read_base_file(task_id, rel)
    new = read_work_file(task_id, rel)
    if old is None and new is None:
        return None
    if old is None:
        status = "added"
        old_lines: List[str] = []
        new_lines = (new or "").splitlines(keepends=True)
    elif new is None:
        status = "deleted"
        old_lines = old.splitlines(keepends=True)
        new_lines = []
    else:
        if old == new:
            return None
        status = "modified"
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)

    udiff = "".join(
        unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}")
    )
    add, dele = _count_hunk_stats(udiff)
    return FileDiff(
        path=rel,
        status=status,
        unified_diff=udiff,
        old_content=old or "",
        new_content=new or "",
        additions=add,
        deletions=dele,
    )


def _list_side(task_id: str, side: str) -> List[str]:
    root = _task_root(task_id) / side
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def list_work_rel_paths(task_id: str) -> List[str]:
    return _list_side(task_id, "work")


def list_base_rel_paths(task_id: str) -> List[str]:
    return _list_side(task_id, "base")


def compute_all_diffs(task_id: str) -> List[FileDiff]:
    paths = set(list_work_rel_paths(task_id)) | set(list_base_rel_paths(task_id))
    diffs: List[FileDiff] = []
    for rel in sorted(paths):
        d = compute_file_diff(task_id, rel)
        if d and d.unified_diff:
            diffs.append(d)
    return diffs


def combined_unified_diff(diffs: List[FileDiff]) -> str:
    parts = [d.unified_diff.rstrip("\n") for d in diffs if d.unified_diff.strip()]
    return ("\n\n".join(parts) + "\n") if parts else ""


def discard_workspace(task_id: str) -> None:
    work = _task_root(task_id) / "work"
    if not work.exists():
        return
    for p in sorted(work.rglob("*"), reverse=True):
        if p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass


def snapshot_to_dict(task_id: str, *, repo: str, ref: str) -> Dict[str, Any]:
    diffs = compute_all_diffs(task_id)
    return {
        "task_id": task_id,
        "repo": repo,
        "ref": ref,
        "workspace_root": str(_task_root(task_id)),
        "files_touched": [d.path for d in diffs],
        "diffs": [
            {
                "path": d.path,
                "status": d.status,
                "unified_diff": d.unified_diff,
                "additions": d.additions,
                "deletions": d.deletions,
            }
            for d in diffs
        ],
        "combined_diff": combined_unified_diff(diffs),
        "stats": {
            "files": len(diffs),
            "additions": sum(d.additions for d in diffs),
            "deletions": sum(d.deletions for d in diffs),
        },
    }


def _file_digest(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def build_publish_manifest(task_id: str) -> Dict[str, Any]:
    """基于当前 work/ 相对 base/ 的待发布文件，计算稳定内容哈希。

    覆盖：将上传的路径 + 文件字节；deleted 记为 tombstone（本阶段 publish 不删远程，
    但仍纳入哈希，防止路径集合漂移）。
    """
    diffs = compute_all_diffs(task_id)
    entries: List[Dict[str, Any]] = []
    for d in diffs:
        if d.status == "deleted":
            entries.append({"path": d.path, "status": "deleted", "sha256": ""})
            continue
        content = read_work_file(task_id, d.path)
        if content is None:
            continue
        entries.append(
            {
                "path": d.path,
                "status": d.status,
                "sha256": _file_digest(content),
                "nbytes": len(content.encode("utf-8")),
            }
        )
    entries.sort(key=lambda e: e["path"])
    canonical = json.dumps(entries, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "task_id": task_id,
        "content_hash": content_hash,
        "files": entries,
        "file_count": len(entries),
    }


def approved_snapshot_dir(task_id: str) -> Path:
    return _task_root(task_id) / "approved_snapshot"


def freeze_approved_snapshot(task_id: str) -> Dict[str, Any]:
    """展示/批准前：固化待发布文件到 approved_snapshot/，并返回 manifest。

    使用 write_bytes 保持与 SHA-256 字节一致（避免 Windows write_text 换行转换）。
    """
    manifest = build_publish_manifest(task_id)
    snap = approved_snapshot_dir(task_id)
    if snap.exists():
        shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir(parents=True, exist_ok=True)
    for ent in manifest.get("files") or []:
        path = ent.get("path") or ""
        if not path or ent.get("status") == "deleted":
            continue
        content = read_work_file(task_id, path)
        if content is None:
            continue
        dest = _abs(_task_root(task_id), "approved_snapshot", path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((content if content is not None else "").encode("utf-8"))
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    (snap / "MANIFEST.json").write_bytes(manifest_bytes)
    return manifest


def load_approved_manifest(task_id: str) -> Optional[Dict[str, Any]]:
    path = approved_snapshot_dir(task_id) / "MANIFEST.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def read_approved_file(task_id: str, rel: str) -> Optional[str]:
    path = _abs(_task_root(task_id), "approved_snapshot", rel)
    if path.is_symlink():
        return None
    if not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _is_unsafe_snapshot_entry(path: Path) -> Optional[str]:
    """返回拒绝原因；安全普通文件返回 None。"""
    if path.is_symlink():
        return "symlink_forbidden"
    if not path.exists():
        return "missing_file"
    if not path.is_file():
        return "not_regular_file"
    # 拒绝设备/管道等（Windows 上 is_file 通常已过滤）
    try:
        if path.stat().st_nlink < 1:
            return "special_file"
    except OSError:
        return "unreadable"
    return None


def list_approved_snapshot_rel_paths(task_id: str) -> Tuple[Optional[List[str]], str]:
    """列出 approved_snapshot 中的内容文件（不含 MANIFEST）。遇符号链接/非法项 fail-closed。

    子目录（如 docs/）是嵌套文件的正常载体，必须跳过，不能当 not_regular_file。
    """
    root = approved_snapshot_dir(task_id)
    if not root.exists():
        return None, "approved_snapshot_missing"
    out: List[str] = []
    seen: set[str] = set()
    for p in sorted(root.rglob("*")):
        if not p.exists() and not p.is_symlink():
            continue
        # 普通目录只是路径容器；符号链接目录仍拒绝
        if p.is_dir() and not p.is_symlink():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "MANIFEST.json" or rel.endswith("/MANIFEST.json"):
            continue
        reason = _is_unsafe_snapshot_entry(p)
        if reason:
            return None, f"{reason}:{rel}"
        try:
            norm = normalize_rel_path(rel)
        except ValueError:
            return None, f"unsafe_path:{rel}"
        if norm in seen:
            return None, f"duplicate_path:{norm}"
        seen.add(norm)
        out.append(norm)
    return sorted(out), "ok"


def compute_diffs_from_approved_snapshot(task_id: str) -> List[FileDiff]:
    """用冻结快照相对 base 计算 Diff（展示与批准必须同源）。"""
    paths_snap, reason = list_approved_snapshot_rel_paths(task_id)
    if paths_snap is None:
        raise ValueError(reason)
    snap_set = set(paths_snap)
    frozen = load_approved_manifest(task_id) or {}
    deleted = {
        normalize_rel_path(str(e.get("path") or ""))
        for e in (frozen.get("files") or [])
        if e.get("status") == "deleted" and e.get("path")
    }
    paths = snap_set | deleted
    # 仅对 Manifest 声明的路径做 Diff；忽略 base 中未变更文件
    diffs: List[FileDiff] = []
    for rel in sorted(paths):
        old = read_base_file(task_id, rel)
        if rel in deleted:
            new: Optional[str] = None
        else:
            new = read_approved_file(task_id, rel)
            if new is None:
                raise ValueError(f"snapshot_missing_file:{rel}")
        if old is None and new is None:
            continue
        if old is None:
            status = "added"
            old_lines: List[str] = []
            new_lines = (new or "").splitlines(keepends=True)
        elif new is None:
            status = "deleted"
            old_lines = old.splitlines(keepends=True)
            new_lines = []
        else:
            if old == new:
                continue
            status = "modified"
            old_lines = old.splitlines(keepends=True)
            new_lines = new.splitlines(keepends=True)
        udiff = "".join(
            unified_diff(old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}")
        )
        add, dele = _count_hunk_stats(udiff)
        if not udiff:
            continue
        diffs.append(
            FileDiff(
                path=rel,
                status=status,
                unified_diff=udiff,
                old_content=old or "",
                new_content=new or "",
                additions=add,
                deletions=dele,
            )
        )
    return diffs


def snapshot_from_approved(task_id: str, *, repo: str, ref: str) -> Dict[str, Any]:
    """展示用快照：Diff 仅来自 approved_snapshot，并附带 content_hash。"""
    frozen = load_approved_manifest(task_id) or {}
    diffs = compute_diffs_from_approved_snapshot(task_id)
    return {
        "task_id": task_id,
        "repo": repo,
        "ref": ref,
        "workspace_root": str(_task_root(task_id)),
        "content_hash": str(frozen.get("content_hash") or ""),
        "files_touched": [d.path for d in diffs],
        "diffs": [
            {
                "path": d.path,
                "status": d.status,
                "unified_diff": d.unified_diff,
                "additions": d.additions,
                "deletions": d.deletions,
            }
            for d in diffs
        ],
        "combined_diff": combined_unified_diff(diffs),
        "stats": {
            "files": len(diffs),
            "additions": sum(d.additions for d in diffs),
            "deletions": sum(d.deletions for d in diffs),
        },
        "source": "approved_snapshot",
    }


def verify_and_load_approved_snapshot(
    task_id: str, expected_hash: str
) -> Tuple[bool, str, Dict[str, str], Dict[str, Any]]:
    """对即将上传的 approved_snapshot 逐文件重算 SHA-256，读入不可变内存副本。

    校验失败 fail-closed；成功时返回 path→content 映射（仅普通文件，不含 deleted）。
    限制：校验与上传之间仍存在进程外替换窗口；因此上传必须只用本函数返回的内存字节，
    不得再次从磁盘 work/ 或未经验证路径读取。
    """
    empty: Dict[str, str] = {}
    if not expected_hash:
        return False, "missing_approved_content_hash", empty, {}

    frozen = load_approved_manifest(task_id)
    if not frozen:
        return False, "manifest_missing_or_corrupt", empty, {}

    manifest_hash = str(frozen.get("content_hash") or "")
    if manifest_hash != expected_hash:
        return False, "manifest_hash_mismatch", empty, frozen

    entries = frozen.get("files")
    if not isinstance(entries, list):
        return False, "manifest_files_invalid", empty, frozen

    expected_paths: Dict[str, Dict[str, Any]] = {}
    for ent in entries:
        if not isinstance(ent, dict):
            return False, "manifest_entry_invalid", empty, frozen
        path = ent.get("path") or ""
        try:
            path = normalize_rel_path(str(path))
        except ValueError:
            return False, f"unsafe_manifest_path:{path}", empty, frozen
        if path in expected_paths:
            return False, f"duplicate_manifest_path:{path}", empty, frozen
        expected_paths[path] = ent

    actual_paths, list_reason = list_approved_snapshot_rel_paths(task_id)
    if actual_paths is None:
        return False, list_reason, empty, frozen

    expected_upload = {
        p for p, e in expected_paths.items() if e.get("status") != "deleted"
    }
    actual_set = set(actual_paths)
    if actual_set != expected_upload:
        missing = sorted(expected_upload - actual_set)
        extra = sorted(actual_set - expected_upload)
        if missing:
            return False, f"snapshot_missing_files:{','.join(missing[:20])}", empty, frozen
        return False, f"snapshot_extra_files:{','.join(extra[:20])}", empty, frozen

    loaded: Dict[str, str] = {}
    recomputed: List[Dict[str, Any]] = []
    for path in sorted(expected_paths.keys()):
        ent = expected_paths[path]
        status = ent.get("status") or "modified"
        if status == "deleted":
            recomputed.append({"path": path, "status": "deleted", "sha256": ""})
            continue
        abs_path = _abs(_task_root(task_id), "approved_snapshot", path)
        unsafe = _is_unsafe_snapshot_entry(abs_path)
        if unsafe:
            return False, f"{unsafe}:{path}", empty, frozen
        try:
            # 以二进制读入再按 utf-8 解码，避免 TOCTOU 窗口内二次读盘不一致
            raw = abs_path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"unreadable:{path}:{exc}", empty, frozen
        digest = hashlib.sha256(raw).hexdigest()
        expected_digest = str(ent.get("sha256") or "")
        if not expected_digest or digest != expected_digest:
            return False, f"file_hash_mismatch:{path}", empty, frozen
        if int(ent.get("nbytes") or -1) >= 0 and int(ent["nbytes"]) != len(raw):
            return False, f"file_size_mismatch:{path}", empty, frozen
        loaded[path] = content
        recomputed.append(
            {
                "path": path,
                "status": status,
                "sha256": digest,
                "nbytes": len(raw),
            }
        )

    recomputed.sort(key=lambda e: e["path"])
    canonical = json.dumps(recomputed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    recomputed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if recomputed_hash != expected_hash:
        return False, "recomputed_content_hash_mismatch", empty, frozen

    return True, "ok", loaded, frozen


def verify_workspace_matches_approved(
    task_id: str, approved_hash: str
) -> Tuple[bool, str, Dict[str, Any]]:
    """发布前：work 哈希 + 快照 Manifest 与批准哈希一致，且快照字节完整。"""
    current = build_publish_manifest(task_id)
    cur_hash = current.get("content_hash") or ""
    if not approved_hash:
        return False, "missing_approved_content_hash", current
    if cur_hash != approved_hash:
        return False, "content_hash_mismatch", current
    frozen = load_approved_manifest(task_id)
    if not frozen or (frozen.get("content_hash") or "") != approved_hash:
        return False, "approved_snapshot_mismatch", current
    ok, reason, _loaded, _ = verify_and_load_approved_snapshot(task_id, approved_hash)
    if not ok:
        return False, reason, current
    return True, "ok", current


def clear_approved_snapshot(task_id: str) -> None:
    snap = approved_snapshot_dir(task_id)
    if snap.exists():
        shutil.rmtree(snap, ignore_errors=True)
