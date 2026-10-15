"""Publish Agent —— 仅在三门审批 + 测试通过 + 内容哈希匹配后，推送已批准快照到 GitHub。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.agents.contracts import AgentEnvelope, utc_now
from app.core.idempotency import build_idempotency_key
from app.services import audit_service, workspace_service as ws
from app.services.github_client import GitHubAPIError, github_client
from app.services.guardrails import PathSecurityError, validate_write_path
from app.services.redis_service import redis_service
from app.services.write_gate import GitHubWriteDenied, permit_github_write

logger = logging.getLogger(__name__)


def format_publish_error(exc_or_text: Any) -> str:
    """把 GitHub API 原始 JSON 错误转成可读中文（尤其是 PAT 权限不足）。"""
    raw = str(exc_or_text or "").strip()
    msg = raw
    status = None
    try:
        start = raw.find("{")
        if start >= 0:
            obj = json.loads(raw[start:])
            if isinstance(obj, dict):
                msg = str(obj.get("message") or msg)
                status = obj.get("status")
    except Exception:  # noqa: BLE001
        pass
    low = msg.lower()
    if (
        "resource not accessible by personal access token" in low
        or (str(status) == "403" and "personal access token" in low)
        or ("403" in raw and "not accessible" in low)
    ):
        return (
            "GitHub Token 权限不足（403）：无法创建分支/提交/PR。"
            "请在 GitHub → Settings → Developer settings → Personal access tokens "
            "为当前仓库开通 Contents: Read and write 与 Pull requests: Read and write"
            "（经典 PAT 需勾选 repo），更新 backend/.env 的 GITHUB_TOKEN 后重启后端，再「失败重试」。"
            f" 原始信息：{msg}"
        )
    if "bad credentials" in low or "requires authentication" in low:
        return (
            "GitHub Token 无效或未配置（401）。请检查 backend/.env 的 GITHUB_TOKEN 后重启。"
            f" 原始信息：{msg}"
        )
    return raw[:800]


def _split_repo(repo: str) -> tuple[str, str]:
    parts = (repo or "").split("/")
    if len(parts) != 2:
        raise ValueError(f"invalid repo: {repo}")
    return parts[0], parts[1]


def _branch_name(task_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9\-]", "", (task_id or "")[:12]) or "task"
    return f"agent/phase3-{safe}"


def evaluate_test_gate(
    test_report: Optional[Dict[str, Any]],
    *,
    allow_untrusted: bool = False,
) -> tuple[bool, str]:
    """测试门禁：无独立可信执行器时默认硬关闭。

    本阶段不得信任状态字典中的 passed/not_run 等字段授权真实发布。
    不引入 Agent 自填的 trusted=true；未来由隔离执行器在可信边界出具证明后再放行。

    allow_untrusted=True（仅本地 ALLOW_UNTRUSTED_PUBLISH）：允许 not_run/unavailable
    继续发布；真正 failed 仍拦截；伪造 passed=True 仍拒绝。
    """
    report = test_report or {}
    status = str(report.get("status") or "").lower()
    if report.get("failed") or status in {"failed", "timeout", "error"}:
        return False, f"test_{status or 'failed'}"
    if report.get("unavailable") or status == "unavailable":
        if allow_untrusted:
            return True, "local_untrusted_bypass_unavailable"
        return False, "test_unavailable"
    if report.get("not_run") or status == "not_run":
        if allow_untrusted:
            return True, "local_untrusted_bypass_not_run"
        return False, "test_not_run"
    # 即便声称 passed=True，无安全执行器证明也一律拒绝（防伪造/注入/旧报告复用）
    if report.get("passed"):
        return False, "untrusted_passed_no_secure_executor"
    if allow_untrusted:
        return True, "local_untrusted_bypass"
    return False, "test_not_passed"


async def _invalidate_publish_approvals(task_id: str, *, reason: str) -> None:
    """校验失败：使当前审批失效，要求重新审批；不自动重新冻结。"""
    try:
        await redis_service.resolve_approval(
            task_id, decision=f"invalidated:{reason}", approval_kind="publish"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invalidate approval failed: %s", exc)
    audit_service.record_security_event(
        action="approval_invalidate",
        result="denied",
        reason=reason,
        task_id=task_id,
    )


async def run_publish(
    *,
    task_id: str,
    repo: str,
    plan_document: Optional[Dict[str, Any]] = None,
    publish_approved: bool = False,
    diff_approved: bool = False,
    plan_confirmed: bool = False,
    base_branch: str = "main",
    approved_content_hash: str = "",
    test_report: Optional[Dict[str, Any]] = None,
) -> AgentEnvelope:
    started = utc_now()
    env = AgentEnvelope(
        task_id=task_id,
        agent_name="publish",
        status="running",
        input_context={
            "repo": repo,
            "publish_approved": publish_approved,
            "diff_approved": diff_approved,
            "plan_confirmed": plan_confirmed,
            "approved_content_hash": (approved_content_hash or "")[:64],
        },
        started_at=started,
    )
    if not (publish_approved and diff_approved and plan_confirmed):
        env.status = "failed"
        env.error = "publish_gate_blocked"
        env.structured_output = {
            "reason": "需要 plan_confirmed + diff_approved + publish_approved",
        }
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="publish_gate",
            result="denied",
            reason="triple_gate",
            task_id=task_id,
            repo=repo,
        )
        return env

    from app.config import get_settings

    settings = get_settings()
    allow_untrusted = bool(settings.allow_untrusted_publish)
    ok_test, test_reason = evaluate_test_gate(
        test_report, allow_untrusted=allow_untrusted
    )
    if not ok_test:
        env.status = "failed"
        env.error = f"test_gate_blocked:{test_reason}"
        env.structured_output = {
            "reason": test_reason,
            "test_report": {
                "status": (test_report or {}).get("status"),
                "passed": (test_report or {}).get("passed"),
                "not_run": (test_report or {}).get("not_run"),
                "unavailable": (test_report or {}).get("unavailable"),
            },
        }
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="test_gate",
            result="denied",
            reason=test_reason,
            task_id=task_id,
            repo=repo,
        )
        return env
    if allow_untrusted and test_reason.startswith("local_untrusted"):
        audit_service.record_security_event(
            action="test_gate",
            result="allowed",
            reason=test_reason,
            task_id=task_id,
            repo=repo,
        )

    # 报告与批准内容绑定（缺失或错绑均拒绝；防旧任务报告复用）
    # 本地 untrusted 旁路：testing 未跑时允许用 approved_content_hash 补齐绑定
    report_hash = str((test_report or {}).get("content_hash") or "")
    if not report_hash and allow_untrusted and approved_content_hash:
        report_hash = approved_content_hash
    if not report_hash:
        env.status = "failed"
        env.error = "test_report_missing_content_hash"
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="test_gate",
            result="denied",
            reason="test_report_missing_content_hash",
            task_id=task_id,
            repo=repo,
        )
        return env
    if not approved_content_hash or report_hash != approved_content_hash:
        env.status = "failed"
        env.error = "test_report_content_mismatch"
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="test_gate",
            result="denied",
            reason="test_report_content_mismatch",
            task_id=task_id,
            repo=repo,
            content_hash=(approved_content_hash or "")[:16],
        )
        return env

    # 权威门禁：approved_snapshot 字节完整性（不信任可变 work/）
    # work 漂移仅审计，不得触发静默重新冻结；快照被改则失效审批。
    current_work = ws.build_publish_manifest(task_id)
    if (current_work.get("content_hash") or "") != approved_content_hash:
        audit_service.record_security_event(
            action="content_hash",
            result="allowed",
            reason="work_drift_ignored_snapshot_authoritative",
            task_id=task_id,
            repo=repo,
            content_hash=(approved_content_hash or "")[:16],
            extra={"work_hash": str(current_work.get("content_hash") or "")[:16]},
        )

    snap_ok, snap_reason, verified_files, frozen = ws.verify_and_load_approved_snapshot(
        task_id, approved_content_hash
    )
    if not snap_ok:
        await _invalidate_publish_approvals(task_id, reason=snap_reason)
        env.status = "failed"
        env.error = f"snapshot_integrity:{snap_reason}"
        env.structured_output = {
            "reason": snap_reason,
            "approved_content_hash": approved_content_hash,
            "reapproval_required": True,
        }
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="snapshot_integrity",
            result="denied",
            reason=snap_reason,
            task_id=task_id,
            repo=repo,
            content_hash=(approved_content_hash or "")[:16],
        )
        return env

    files = [f for f in (frozen.get("files") or []) if f.get("status") != "deleted"]
    if not files or not verified_files:
        env.status = "failed"
        env.error = "no_approved_snapshot_files"
        env.finished_at = utc_now()
        return env

    # 发布前强制敏感路径护栏（即使三门审批已通过也不得绕过）
    for ent in files:
        path = str(ent.get("path") or "")
        try:
            validate_write_path(path)
        except PathSecurityError as exc:
            await _invalidate_publish_approvals(
                task_id, reason=f"sensitive_path:{exc.code}"
            )
            env.status = "failed"
            env.error = f"sensitive_path_blocked:{exc.code}"
            env.structured_output = {
                "reason": exc.code,
                "reapproval_required": True,
            }
            env.finished_at = utc_now()
            audit_service.record_security_event(
                action="path_guardrail",
                result="denied",
                reason=exc.code,
                task_id=task_id,
                repo=repo,
            )
            return env
    for vpath in verified_files:
        try:
            validate_write_path(str(vpath))
        except PathSecurityError as exc:
            await _invalidate_publish_approvals(
                task_id, reason=f"sensitive_path:{exc.code}"
            )
            env.status = "failed"
            env.error = f"sensitive_path_blocked:{exc.code}"
            env.finished_at = utc_now()
            audit_service.record_security_event(
                action="path_guardrail",
                result="denied",
                reason=exc.code,
                task_id=task_id,
                repo=repo,
            )
            return env

    # 持久化幂等：无 Redis 时默认拒绝；本地 ALLOW_PUBLISH_WITHOUT_REDIS 可用内存幂等
    if getattr(redis_service, "fallback", True) and not bool(
        settings.allow_publish_without_redis
    ):
        env.status = "failed"
        env.error = "idempotency_unavailable_no_redis"
        env.structured_output = {
            "reason": "Redis 不可用，无法保证发布幂等，已拒绝远程写入",
        }
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="publish_gate",
            result="denied",
            reason="no_redis_idempotency",
            task_id=task_id,
            repo=repo,
        )
        return env
    if getattr(redis_service, "fallback", True) and settings.allow_publish_without_redis:
        audit_service.record_security_event(
            action="publish_gate",
            result="allowed",
            reason="memory_idempotency_local",
            task_id=task_id,
            repo=repo,
        )

    owner, name = _split_repo(repo)
    branch = _branch_name(task_id)
    title = (plan_document or {}).get("title") or f"agent: workspace changes {task_id[:8]}"
    body = (
        "## Agent Publish\n\n"
        f"- task: `{task_id}`\n"
        f"- content_hash: `{approved_content_hash}`\n"
        f"- files: {', '.join(f.get('path') for f in files)}\n"
        "- source: approved_snapshot (hash-bound, memory-verified)\n"
    )

    idem = build_idempotency_key(
        "publish_flow",
        repo=repo,
        task_id=task_id,
        branch=branch,
        content_hash=approved_content_hash,
    )
    lock = await redis_service.acquire_idempotency(idem, ttl=1800)
    if not lock.acquired and lock.cached_value:
        try:
            cached = json.loads(lock.cached_value)
        except Exception:  # noqa: BLE001
            cached = {"raw": lock.cached_value}
        env.status = "succeeded"
        env.structured_output = {**cached, "deduplicated": True}
        env.artifacts = cached
        env.finished_at = utc_now()
        audit_service.record_security_event(
            action="publish_idempotency",
            result="allowed",
            reason="cache_hit",
            task_id=task_id,
            repo=repo,
            content_hash=approved_content_hash[:16],
        )
        return env

    steps: List[Dict[str, Any]] = []
    try:
        with permit_github_write(
            task_id=task_id,
            reason="publish_agent",
            content_hash=approved_content_hash,
            repo=repo,
        ):
            try:
                br = await github_client.create_branch(
                    owner, name, branch, from_branch=base_branch
                )
                steps.append({"step": "create_branch", "ok": True, "branch": branch, "data": br})
            except GitHubAPIError as exc:
                if exc.status in {422, 409} or "already exists" in (exc.message or "").lower():
                    steps.append(
                        {"step": "create_branch", "ok": True, "branch": branch, "exists": True}
                    )
                else:
                    raise

            commit_shas: List[str] = []
            # 仅上传已验证内存快照；禁止回退读 work/ 或再次读盘
            for ent in files:
                path = ent.get("path")
                if not path:
                    continue
                # 远程写入前最后一次路径护栏（审批后也不可绕过）
                try:
                    path = validate_write_path(str(path))
                except PathSecurityError as exc:
                    raise RuntimeError(f"sensitive_path_blocked:{exc.code}") from exc
                content = verified_files.get(path)
                if content is None:
                    # 兼容规范化前后键
                    content = verified_files.get(str(ent.get("path") or ""))
                if content is None:
                    raise RuntimeError(f"verified_snapshot missing file: {path}")
                sha = None
                try:
                    cur = await github_client.get_file_content(owner, name, path, ref=branch)
                    sha = cur.get("sha")
                except GitHubAPIError:
                    sha = None
                upd = await github_client.update_file(
                    owner,
                    name,
                    path,
                    content,
                    message=f"agent: update {path}",
                    branch=branch,
                    sha=sha or "",
                )
                sha_out = (upd.get("commit") or {}).get("sha") or upd.get("content", {}).get("sha")
                if sha_out:
                    commit_shas.append(str(sha_out))
                steps.append({"step": "update_file", "ok": True, "path": path, "sha": sha_out})

            from app.tools.github_tools import create_pr

            pr_raw = await create_pr.ainvoke(
                {
                    "repo": repo,
                    "title": title[:200],
                    "body": body,
                    "head": branch,
                    "base": base_branch,
                }
            )
            try:
                pr = json.loads(pr_raw) if isinstance(pr_raw, str) else pr_raw
            except Exception:  # noqa: BLE001
                pr = {"ok": False, "raw": str(pr_raw)[:500]}
            steps.append({"step": "create_pr", "result": pr})

            if not pr.get("ok"):
                await redis_service.release_idempotency(idem)
                env.status = "failed"
                env.error = str(pr.get("error") or "create_pr_failed")
                env.structured_output = {"steps": steps, "pr": pr}
                env.finished_at = utc_now()
                return env

            if pr.get("deduplicated") and isinstance(pr.get("cached"), str):
                try:
                    pr = {**json.loads(pr["cached"]), "deduplicated": True}
                except Exception:  # noqa: BLE001
                    pass

            result = {
                "ok": True,
                "branch": branch,
                "pr_url": pr.get("html_url"),
                "pr_number": pr.get("number"),
                "commit_sha": commit_shas[-1] if commit_shas else None,
                "content_hash": approved_content_hash,
                "steps": steps,
                "pr": pr,
            }
            await redis_service.set_idempotency_result(
                idem, json.dumps(result, ensure_ascii=False), ttl=1800
            )
            env.status = "succeeded"
            env.structured_output = result
            env.artifacts = {
                "pr_url": result.get("pr_url"),
                "pr_number": result.get("pr_number"),
                "branch": branch,
                "commit_sha": result.get("commit_sha"),
                "content_hash": approved_content_hash,
            }
            env.finished_at = utc_now()
            audit_service.record_security_event(
                action="publish",
                result="allowed",
                reason="published",
                task_id=task_id,
                repo=repo,
                content_hash=approved_content_hash[:16],
            )
            return env
    except GitHubWriteDenied as exc:
        await redis_service.release_idempotency(idem)
        env.status = "failed"
        env.error = str(exc)[:500]
        env.finished_at = utc_now()
        return env
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish failed")
        await redis_service.release_idempotency(idem)
        env.status = "failed"
        env.error = format_publish_error(exc)[:900]
        env.structured_output = {"steps": steps, "raw_error": str(exc)[:500]}
        env.finished_at = utc_now()
        return env
