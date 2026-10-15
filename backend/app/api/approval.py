"""审批接口 —— 用户批准/拒绝后从 Redis 检查点 resume。"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.agent.graph import resume_with_approval
from app.core.security_auth import require_operator
from app.models.schemas import ApprovalDecision, ApprovalItem, ApprovalRequest
from app.services import audit_service
from app.services.github_client import GitHubAPIError, github_client
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/approval", tags=["approval"])


@router.get("/pending", response_model=list[ApprovalItem])
async def list_pending(_auth: None = Depends(require_operator)):
    items = await redis_service.list_pending_approvals()
    out = []
    for it in items:
        out.append(
            ApprovalItem(
                thread_id=it.get("thread_id") or "",
                operation=it.get("operation") or "",
                risk_level=it.get("risk_level") or "high",
                repo=it.get("repo") or "",
                summary=it.get("summary") or "",
                diff=it.get("diff") or "",
                payload=it.get("payload") or {},
                status=it.get("status") or "pending",
                approval_kind=it.get("approval_kind")
                or (it.get("payload") or {}).get("approval_kind")
                or "publish",
                task_id=it.get("task_id") or it.get("thread_id") or "",
                phase=it.get("phase") or (it.get("payload") or {}).get("phase") or "",
            )
        )
    return out


@router.get("/history")
async def list_history(
    kind: str = Query("", description="approval | commit | 空=全部"),
    repo: str = Query(""),
    limit: int = Query(80, ge=1, le=200),
    _auth: None = Depends(require_operator),
):
    """本地持久化的审批/提交审计（backend/.data/audit_log.json）。"""
    return {
        "ok": True,
        "items": audit_service.list_events(kind=kind, repo=repo, limit=limit),
    }


@router.get("/commits/{owner}/{repo}")
async def list_repo_commits(
    owner: str,
    repo: str,
    sha: str = Query("main"),
    per_page: int = Query(20, ge=1, le=50),
    _auth: None = Depends(require_operator),
):
    """GitHub 远程提交 + 本地编辑器提交审计，附带回滚链接。"""
    remote: list = []
    err = ""
    try:
        raw = await github_client.list_commits(owner, repo, sha=sha, per_page=per_page)
        for c in raw:
            commit = c.get("commit") or {}
            author = commit.get("author") or {}
            sha_full = c.get("sha") or ""
            remote.append(
                {
                    "sha": sha_full,
                    "short_sha": sha_full[:7],
                    "message": (commit.get("message") or "").split("\n", 1)[0],
                    "author": author.get("name") or "",
                    "date": author.get("date") or "",
                    "html_url": c.get("html_url") or "",
                    "rollback": {
                        "commit_url": c.get("html_url") or "",
                        "compare_url": f"https://github.com/{owner}/{repo}/compare/{sha_full}^...{sha_full}",
                        "hint": "在 GitHub 打开该 commit → Browse files 可看内容；已合并用 Revert PR 或 git revert。",
                    },
                }
            )
    except GitHubAPIError as exc:
        err = exc.message[:300]
        logger.warning("list commits failed: %s", err)

    local = audit_service.list_events(kind="commit", repo=f"{owner}/{repo}", limit=per_page)
    return {
        "ok": True,
        "repo": f"{owner}/{repo}",
        "sha": sha,
        "remote": remote,
        "local": local,
        "error": err,
    }


@router.post("/{tid}")
async def submit_approval(
    tid: str,
    body: ApprovalRequest,
    request: Request,
    _auth: None = Depends(require_operator),
):
    from app.services.approval_gate import validate_approval_resume

    pending = await redis_service.get_pending_approval(tid)
    resolved = await redis_service.get_resolved_approval(tid)

    ok, reason, pending = validate_approval_resume(
        tid,
        decision=body.decision.value,
        approval_kind=body.approval_kind,
        task_id=body.task_id,
        pending=pending,
        resolved=resolved,
    )
    if not ok:
        raise HTTPException(status_code=409, detail=reason)

    audit_service.record_approval_decision(
        thread_id=tid,
        decision=body.decision.value,
        comment=body.comment or "",
    )

    decision = {
        "decision": body.decision.value,
        "comment": body.comment or "",
        "approval_kind": (body.approval_kind or (pending or {}).get("approval_kind") or ""),
        "task_id": body.task_id or tid,
    }

    async def event_generator():
        try:
            async for ev in resume_with_approval(tid, decision):
                if await request.is_disconnected():
                    break
                # 批准后若出现 PR/分支信息，补记到审计
                if ev.get("event") == "tool_result" or ev.get("event") == "final":
                    data = ev.get("data") or {}
                    pr_url = data.get("pr_url") or ""
                    if pr_url or data.get("branch"):
                        audit_service.update_event(
                            tid,
                            {
                                "pr_url": pr_url,
                                "branch": data.get("branch") or "",
                                "commit_sha": data.get("commit_sha") or "",
                                "result_summary": str(data)[:500],
                            },
                        )
                payload = json.dumps(ev, ensure_ascii=False, default=str)
                yield f"event: {ev.get('event', 'message')}\ndata: {payload}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("approval resume failed")
            err = json.dumps({"event": "error", "data": {"message": str(exc)}, "thread_id": tid})
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Thread-Id": tid},
    )


@router.get("/{tid}")
async def get_approval(tid: str, _auth: None = Depends(require_operator)):
    data = await redis_service.get_pending_approval(tid)
    if not data:
        raise HTTPException(status_code=404, detail="approval not found")
    return data
