"""仓库浏览 / RAG 索引 / 在线编辑 / AI 改码 —— 单体与 repo_svc 共用。"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.core.security_auth import require_operator
from app.models.schemas import (
    AiEditRequest,
    AiEditResponse,
    FileContentResponse,
    FileSaveRequest,
    FileSaveResponse,
    RepoTreeNode,
    RepoTreeResponse,
)
from app.services import audit_service, git_ssh_writer
from app.services.ai_edit import ai_edit_buffer, ai_edit_buffer_stream
from app.services.git_ssh_writer import GitSSHError
from app.services.github_client import GitHubAPIError, github_client
from app.services.guardrails import check_branch_isolation, check_path_whitelist
from app.services.code_graph_extract import is_graphable
from app.services.graph_rag import graph_rag_service
from app.services.rag_service import rag_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["repos"])


@router.get("/api/repos/{owner}/{repo}/tree", response_model=RepoTreeResponse)
async def repo_tree(
    owner: str,
    repo: str,
    branch: str = Query(default="main"),
    _auth: None = Depends(require_operator),
):
    try:
        data = await github_client.get_repo_tree(owner, repo, branch=branch)
    except GitHubAPIError as exc:
        if exc.status == 409 and "empty" in (exc.message or "").lower():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"仓库 {owner}/{repo} 是空的，还没有任何提交。"
                    "请先添加 README，或点对话让 Agent 初始化文件。"
                ),
            ) from exc
        raise HTTPException(status_code=exc.status or 502, detail=exc.message[:500]) from exc
    nodes = []
    for t in data.get("tree", [])[:2000]:
        nodes.append(
            RepoTreeNode(
                path=t.get("path", ""),
                type="dir" if t.get("type") == "tree" else "file",
                sha=t.get("sha"),
                size=t.get("size"),
            )
        )
    return RepoTreeResponse(repo=f"{owner}/{repo}", branch=branch, nodes=nodes)


@router.post("/api/repos/{owner}/{repo}/bootstrap")
async def bootstrap_repo(owner: str, repo: str, _auth: None = Depends(require_operator)):
    """已禁用：直写 main 绕过审批。请通过对话多 Agent 流程创建变更。"""
    from app.services import audit_service

    audit_service.record_security_event(
        action="remote_write_api",
        result="denied",
        reason="bootstrap_disabled",
        operation="bootstrap_repo",
        repo=f"{owner}/{repo}",
    )
    raise HTTPException(
        status_code=403,
        detail=(
            "bootstrap 已禁用：禁止未经审批直写 GitHub。"
            "请在对话页提交需求，经计划/Diff/测试/发布审批后由 Publish Agent 写入。"
        ),
    )


@router.post("/api/repos/{owner}/{repo}/index")
async def index_repo_file(
    owner: str,
    repo: str,
    path: str = Query(...),
    ref: str = Query(default="main"),
    _auth: None = Depends(require_operator),
):
    """将单个文件写入 RAG 索引（演示用；生产可换成全库 crawler）。"""
    try:
        file_data = await github_client.get_file_content(owner, repo, path, ref=ref)
    except GitHubAPIError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=exc.message[:500]) from exc
    content = file_data.get("content") or ""
    repo_full = f"{owner}/{repo}"
    try:
        result = await rag_service.index_file(repo_full, path, content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"RAG 索引失败: {exc}") from exc
    graph_info: dict = {}
    if is_graphable(path) and not result.get("skipped"):
        try:
            graph_info = graph_rag_service.index_source(repo_full, path, content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph index failed: %s", exc)
            graph_info = {"ok": False, "error": str(exc)}
    n = int(result.get("chunks") or 0)
    clean = result.get("clean") or {}
    redacted = clean.get("redacted") or []
    noise = clean.get("noise_stripped") or []
    clean_bits = []
    if redacted:
        clean_bits.append("脱敏 " + ",".join(redacted))
    if noise:
        clean_bits.append("去噪 " + ",".join(noise))
    if clean.get("skipped"):
        detail = f"已跳过索引（{clean.get('skip_reason') or 'cleaned empty'}）"
    else:
        detail = f"已清洗并索引 {n} chunks（模式: {rag_service.mode}）"
        if clean_bits:
            detail += "；" + "；".join(clean_bits)
        if rag_service.mode == "local":
            detail += "；当前为本地降级"
        if graph_info.get("ok") and not graph_info.get("skipped"):
            st = graph_info.get("stats") or {}
            detail += f"；图谱 +{graph_info.get('edges_added', 0)} 边（共 {st.get('nodes', 0)} 节点）"
    return {
        "ok": True,
        "chunks": n,
        "path": result.get("path") or path,
        "skipped": bool(result.get("skipped")),
        "clean": clean,
        "graph": graph_info,
        "rag_mode": rag_service.mode,
        "trace_id": uuid.uuid4().hex,
        "detail": detail,
    }


@router.get("/api/repos/{owner}/{repo}/graph")
async def repo_graph_summary(
    owner: str, repo: str, _auth: None = Depends(require_operator)
):
    """代码知识图谱摘要（GraphRAG）。"""
    return {"ok": True, **graph_rag_service.summary(f"{owner}/{repo}")}


@router.get("/api/repos/{owner}/{repo}/graph/viz")
async def repo_graph_viz(
    owner: str,
    repo: str,
    max_nodes: int = Query(default=60, ge=5, le=200),
    _auth: None = Depends(require_operator),
):
    """图谱可视化节点/边（前端画布用）。"""
    return {"ok": True, **graph_rag_service.viz(f"{owner}/{repo}", max_nodes=max_nodes)}


def _iter_local_app_sources(limit: int) -> list[tuple[str, str]]:
    """演示回退：仓库无源码时，用本机 backend/app 构建图谱。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]  # backend/app
    out: list[tuple[str, str]] = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root.parent).as_posix()  # app/...
        if not is_graphable(rel):
            continue
        if any(part in {"__pycache__", ".venv", "node_modules"} for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        out.append((rel, text))
        if len(out) >= limit:
            break
    return out


@router.post("/api/repos/{owner}/{repo}/graph/index")
async def build_repo_graph(
    owner: str,
    repo: str,
    ref: str = Query(default="main"),
    limit: int = Query(default=60, ge=1, le=200),
    _auth: None = Depends(require_operator),
):
    """扫描仓库树，对可解析源码构建/更新依赖图谱（演示 GraphRAG）。

    若远程仓库没有 .py/.ts 等文件（常见 demo 空仓），自动回退索引本机 backend/app。
    """
    repo_full = f"{owner}/{repo}"
    paths: list[str] = []
    try:
        tree = await github_client.get_repo_tree(owner, repo, branch=ref)
        paths = [
            str(t.get("path") or "")
            for t in (tree.get("tree") or [])
            if t.get("type") == "blob" and is_graphable(str(t.get("path") or ""))
        ][:limit]
    except GitHubAPIError as exc:
        logger.warning("graph tree failed, will try local fallback: %s", exc.message[:200])

    indexed = 0
    errors = 0
    last_stats: dict = {}
    source = "github"
    for path in paths:
        try:
            file_data = await github_client.get_file_content(owner, repo, path, ref=ref)
            content = file_data.get("content") or ""
            info = graph_rag_service.index_source(repo_full, path, content)
            if info.get("ok") and not info.get("skipped"):
                indexed += 1
                last_stats = info.get("stats") or last_stats
            try:
                await rag_service.index_file(repo_full, path, content)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph build %s failed: %s", path, exc)
            errors += 1

    if indexed == 0:
        source = "local-app"
        for path, content in _iter_local_app_sources(limit):
            try:
                info = graph_rag_service.index_source(repo_full, path, content)
                if info.get("ok") and not info.get("skipped"):
                    indexed += 1
                    last_stats = info.get("stats") or last_stats
                try:
                    await rag_service.index_file(repo_full, path, content)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("local graph %s failed: %s", path, exc)
                errors += 1

    summary = graph_rag_service.summary(repo_full)
    st = summary.get("stats") or last_stats
    detail = (
        f"GraphRAG 已构建（源: {source}）：成功 {indexed} 文件，"
        f"节点 {st.get('nodes', 0)}，边 {st.get('edges', 0)}"
    )
    if source == "local-app":
        detail += "；远程仓无源码，已用本机 backend/app 演示依赖图谱"
    return {
        "ok": True,
        "repo": repo_full,
        "scanned": len(paths) if source == "github" else indexed,
        "indexed": indexed,
        "errors": errors,
        "source": source,
        "stats": st,
        "detail": detail,
    }


@router.get("/api/repos/{owner}/{repo}/graph/search")
async def graph_search(
    owner: str,
    repo: str,
    q: str = Query(..., min_length=1, max_length=500),
    top_k: int = Query(default=5, ge=1, le=20),
    _auth: None = Depends(require_operator),
):
    """图谱增强检索：依赖扩展 + 混合 RAG。"""
    text = await graph_rag_service.format_answer(f"{owner}/{repo}", q, top_k=top_k)
    raw = await graph_rag_service.graph_search(f"{owner}/{repo}", q, top_k=top_k)
    return {"ok": True, "answer": text, "graph": raw.get("graph"), "chunks": raw.get("chunks")}


@router.get("/api/repos/{owner}/{repo}/file", response_model=FileContentResponse)
async def get_repo_file(
    owner: str,
    repo: str,
    path: str = Query(..., min_length=1),
    ref: str = Query(default="main"),
    _auth: None = Depends(require_operator),
):
    """在线编辑器：拉取文件内容与 SHA。"""
    try:
        data = await github_client.get_file_content(owner, repo, path, ref=ref)
    except GitHubAPIError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=exc.message[:500]) from exc
    if data.get("type") == "dir":
        raise HTTPException(status_code=400, detail="路径是目录，请指定文件")
    return FileContentResponse(
        repo=f"{owner}/{repo}",
        path=path,
        ref=ref,
        sha=data.get("sha") or "",
        content=data.get("content") or "",
        html_url=data.get("html_url"),
        size=data.get("size"),
    )


@router.put("/api/repos/{owner}/{repo}/file", response_model=FileSaveResponse)
async def save_repo_file(
    owner: str,
    repo: str,
    body: FileSaveRequest,
    _auth: None = Depends(require_operator),
):
    """编辑器远程保存已禁用：禁止绕过 plan/Diff/publish 审批直写 GitHub。

    仍保留接口以便前端收到明确 403；请浏览/本地编辑后走对话多 Agent 发布。
    """
    audit_service.record_security_event(
        action="remote_write_api",
        result="denied",
        reason="editor_direct_write_disabled",
        operation="save_repo_file",
        repo=f"{owner}/{repo}",
        extra={
            "path": (body.path or "")[:200],
            "open_pr": bool(getattr(body, "open_pr", False)),
        },
    )
    raise HTTPException(
        status_code=403,
        detail=(
            "编辑器直写 GitHub 已禁用（含 open_pr）。"
            "请在对话中提交修改需求，经计划确认 → Diff 审批 → 测试门禁 → 发布审批后，"
            "由 Publish Agent 写入远程仓库。"
        ),
    )


@router.post("/api/repos/{owner}/{repo}/ai-edit", response_model=AiEditResponse)
async def ai_edit_file(
    owner: str,
    repo: str,
    body: AiEditRequest,
    _auth: None = Depends(require_operator),
):
    """在线编辑器：AI 按指令改写缓冲区（不落盘，非流式兼容接口）。"""
    try:
        edited = await ai_edit_buffer(
            path=body.path, content=body.content, instruction=body.instruction
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"AI 编辑失败: {exc}") from exc
    return AiEditResponse(
        ok=True,
        path=body.path,
        content=edited,
        detail=f"{owner}/{repo} 缓冲区已由 AI 改写，请确认后保存上传",
    )


@router.post("/api/repos/{owner}/{repo}/ai-edit/stream")
async def ai_edit_file_stream(
    owner: str,
    repo: str,
    body: AiEditRequest,
    request: Request,
    _auth: None = Depends(require_operator),
):
    """AI 改代码 SSE：event=start|delta|done|error，完成后前端可点「保存上传」。"""

    async def event_generator():
        try:
            async for ev in ai_edit_buffer_stream(
                path=body.path,
                content=body.content,
                instruction=body.instruction,
            ):
                if await request.is_disconnected():
                    break
                name = ev.get("event", "message")
                payload = {
                    "event": name,
                    "repo": f"{owner}/{repo}",
                    "path": body.path,
                    "data": ev.get("data") or {},
                }
                yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            err = {"event": "error", "data": {"message": str(exc)}}
            yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"

    origin = request.headers.get("origin") or "http://localhost:5173"
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": origin,
        },
    )
