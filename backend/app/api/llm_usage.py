"""LLM 用量查询 API —— 全部需要 Operator 权限。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security_auth import require_operator
from app.services.llm_usage import aggregate as agg
from app.services.llm_usage import store as usage_store

router = APIRouter(prefix="/api/llm-usage", tags=["llm-usage"])


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid datetime: {raw}") from exc


@router.get("/health")
async def usage_store_health(_auth: None = Depends(require_operator)):
    ok = await usage_store.ensure_schema()
    return {
        "ok": True,
        "storage_backend": "postgres" if ok else "json",
        "postgres_ready": ok,
        "note": (
            "Postgres 为主存储；不可用时降级 JSON。"
            "恢复 Postgres 后不会自动合并 JSON，需显式回灌。"
        ),
    }


@router.get("/calls")
async def list_calls(
    since: Optional[str] = None,
    until: Optional[str] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    task_id: str = "",
    status: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _auth: None = Depends(require_operator),
):
    items, total, backend = await usage_store.query_records(
        since=_parse_dt(since),
        until=_parse_dt(until),
        provider=provider,
        model=model,
        module_name=module_name,
        task_id=task_id,
        status=status,
        offset=offset,
        limit=limit,
    )
    return {
        "ok": True,
        "storage_backend": backend,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [agg.serialize_record(i) for i in items],
        "display_hints": {
            "null_tokens": "未知 / usage 不可用",
            "null_cost": "未知价格或 Token 不可用，非 0 元",
            "cost": "估算费用（CNY），非账单实付",
        },
    }


@router.get("/calls/{call_id}")
async def get_call(call_id: str, _auth: None = Depends(require_operator)):
    row = await usage_store.get_record(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="call not found")
    return {
        "ok": True,
        "storage_backend": row.get("storage_backend") or usage_store.storage_mode(),
        "call": agg.serialize_record(row),
    }


@router.get("/summary")
async def summary(
    since: Optional[str] = None,
    until: Optional[str] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    task_id: str = "",
    _auth: None = Depends(require_operator),
):
    data = await agg.overview(
        since=_parse_dt(since),
        until=_parse_dt(until),
        provider=provider,
        model=model,
        module_name=module_name,
        task_id=task_id,
    )
    return {"ok": True, **data}


@router.get("/by-model")
async def by_model(
    since: Optional[str] = None,
    until: Optional[str] = None,
    provider: str = "",
    module_name: str = "",
    task_id: str = "",
    _auth: None = Depends(require_operator),
):
    data = await agg.by_model(
        since=_parse_dt(since),
        until=_parse_dt(until),
        provider=provider,
        module_name=module_name,
        task_id=task_id,
    )
    return {"ok": True, **data}


@router.get("/by-module")
async def by_module(
    since: Optional[str] = None,
    until: Optional[str] = None,
    provider: str = "",
    model: str = "",
    task_id: str = "",
    _auth: None = Depends(require_operator),
):
    data = await agg.by_module(
        since=_parse_dt(since),
        until=_parse_dt(until),
        provider=provider,
        model=model,
        task_id=task_id,
    )
    return {"ok": True, **data}


@router.get("/trends/daily")
async def trends_daily(
    since: Optional[str] = None,
    until: Optional[str] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    _auth: None = Depends(require_operator),
):
    data = await agg.daily_trends(
        since=_parse_dt(since),
        until=_parse_dt(until),
        provider=provider,
        model=model,
        module_name=module_name,
    )
    return {"ok": True, **data}


@router.get("/failures")
async def failures(
    since: Optional[str] = None,
    until: Optional[str] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    task_id: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _auth: None = Depends(require_operator),
):
    # 拉一批后过滤非 ok（status 多值时用内存过滤）
    rows, backend = await usage_store.fetch_all_for_agg(
        since=_parse_dt(since),
        until=_parse_dt(until),
        provider=provider,
        model=model,
        module_name=module_name,
        task_id=task_id,
        max_rows=5000,
    )
    failed = [r for r in rows if r.get("status") and r.get("status") != "ok"]
    total = len(failed)
    page = failed[offset : offset + limit]
    retries = sum(int(r.get("retry_count") or 0) for r in failed)
    return {
        "ok": True,
        "storage_backend": backend,
        "total": total,
        "retry_count_sum": retries,
        "offset": offset,
        "limit": limit,
        "items": [agg.serialize_record(i) for i in page],
    }


@router.get("/tasks/{task_id}")
async def task_usage(task_id: str, _auth: None = Depends(require_operator)):
    data = await agg.task_detail(task_id)
    return {"ok": True, **data}
