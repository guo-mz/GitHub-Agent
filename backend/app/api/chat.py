"""SSE 流式对话 —— 每个节点事件实时推给前端；任务状态持久化可查询。"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.graph import stream_agent
from app.core.runtime_mode import assert_hitl_task_allowed
from app.core.security_auth import require_operator
from app.models.schemas import ChatRequest
from app.services import task_run_service as trs
from app.services.redis_service import redis_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class RetryRequest(BaseModel):
    """失败/取消后重试：新开 thread，避免重复已成功的外部写操作。"""

    comment: str = Field(default="", max_length=500)


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    _auth: None = Depends(require_operator),
):
    from app.config import get_settings

    thread_id = body.thread_id or uuid.uuid4().hex
    repo = (body.repo or "").strip()
    if not repo or repo in {"owner/repo", "owner/name"}:
        repo = (get_settings().default_repo or "guo-mz/-").strip()
    # 新任务依赖 interrupt/checkpoint；无耐久 checkpoint 时拒绝（除非显式 ephemeral）
    if not body.resume:
        assert_hitl_task_allowed(action="chat_stream")
    await redis_service.cache_session(
        thread_id,
        {"repo": repo, "last_message": body.message},
    )

    # 非 resume：新建任务；若 retry 已预创建 queued 记录则复用，避免重复条目
    if not body.resume:
        existing = trs.get_run_by_thread(thread_id)
        reuse = (
            existing
            and existing.get("status") == "queued"
            and not existing.get("cancel_requested")
            and (existing.get("message") or "") == (body.message or "")
        )
        if not reuse:
            trs.create_run(thread_id=thread_id, repo=repo, message=body.message)

    async def event_generator():
        try:
            # 首包：当前任务快照，便于前端立刻渲染
            snap = trs.public_view(trs.get_run_by_thread(thread_id))
            if snap:
                yield (
                    f"event: task_status\ndata: "
                    f"{json.dumps({'event': 'task_status', 'data': snap, 'thread_id': thread_id, 'seq': snap.get('event_seq')}, ensure_ascii=False, default=str)}\n\n"
                )

            async for ev in stream_agent(
                message=body.message,
                repo=repo,
                thread_id=thread_id,
                resume=body.resume,
            ):
                if await request.is_disconnected():
                    # 客户端断开：请求取消后续节点（在途 LLM 可能仍会结束）
                    trs.request_cancel(thread_id, reason="client_disconnected")
                    break
                if trs.is_cancel_requested(thread_id) and ev.get("event") not in {
                    "task_status",
                    "error",
                    "done",
                }:
                    # 仍会把取消后的终态事件送出
                    pass
                payload = json.dumps(ev, ensure_ascii=False, default=str)
                event_name = ev.get("event", "message")
                yield f"event: {event_name}\ndata: {payload}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("SSE error")
            trs.apply_terminal(thread_id, status="failed", error=str(exc), final_note=str(exc))
            err = json.dumps(
                {"event": "error", "data": {"message": str(exc)}, "thread_id": thread_id},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {err}\n\n"
            snap = trs.public_view(trs.get_run_by_thread(thread_id))
            if snap:
                yield (
                    f"event: task_status\ndata: "
                    f"{json.dumps({'event': 'task_status', 'data': snap, 'thread_id': thread_id}, ensure_ascii=False, default=str)}\n\n"
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Thread-Id": thread_id,
            "Access-Control-Allow-Origin": request.headers.get("origin")
            or "http://localhost:5173",
        },
    )


@router.get("/tasks")
async def list_tasks(
    repo: str = "",
    limit: int = 40,
    _auth: None = Depends(require_operator),
):
    items = [trs.public_view(r) for r in trs.list_runs(repo=repo, limit=limit)]
    return {"ok": True, "items": [i for i in items if i]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, _auth: None = Depends(require_operator)):
    run = trs.get_run(task_id) or trs.get_run_by_thread(task_id)
    view = trs.public_view(run)
    if not view:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task": view}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _auth: None = Depends(require_operator)):
    run = trs.get_run(task_id) or trs.get_run_by_thread(task_id)
    if not run:
        raise HTTPException(status_code=404, detail="task not found")
    if run.get("status") in trs.TERMINAL:
        return {
            "ok": True,
            "task": trs.public_view(run),
            "note": "任务已结束，无需取消",
        }
    updated = trs.request_cancel(str(run.get("thread_id") or task_id), reason="user_cancel")
    return {
        "ok": True,
        "task": trs.public_view(updated),
        "note": (updated or {}).get("cancel_note")
        or "已请求取消；在途 LLM/HTTP 调用可能无法立即中断",
    }


@router.post("/tasks/{task_id}/retry")
async def retry_task(
    task_id: str,
    body: RetryRequest | None = None,
    _auth: None = Depends(require_operator),
):
    assert_hitl_task_allowed(action="chat_retry")
    prev = trs.get_run(task_id) or trs.get_run_by_thread(task_id)
    if not prev:
        raise HTTPException(status_code=404, detail="task not found")
    ok, reason = trs.can_retry(prev)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)
    try:
        new_run = trs.create_retry_run(prev)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "task": trs.public_view(new_run),
        "message": prev.get("message"),
        "repo": prev.get("repo"),
        "note": "已创建新任务线程；请用返回的 thread_id 重新发起 /api/chat/stream，不会复用已成功的 PR",
    }
