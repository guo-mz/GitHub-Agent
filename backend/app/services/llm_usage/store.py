"""LLM 调用记录持久化：Postgres 优先，JSON 降级。

恢复 Postgres 后不自动合并 JSON；如需回灌须显式调用 backfill 并去重。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[3] / ".data" / "llm_calls.json"
_LOCK = threading.RLock()
_MAX_JSON = 5000

_engine: Optional[AsyncEngine] = None
_session_factory = None
_pg_ready: Optional[bool] = None
_schema_ensured = False

DDL = """
CREATE TABLE IF NOT EXISTS llm_call_records (
    id TEXT PRIMARY KEY,
    call_group_id TEXT,
    attempt_index INT DEFAULT 0,
    trace_id TEXT,
    task_id TEXT,
    conversation_id TEXT,
    module_name TEXT,
    node_name TEXT,
    provider TEXT,
    requested_model TEXT,
    actual_model TEXT,
    call_type TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    latency_ms INT,
    time_to_first_token_ms INT,
    input_tokens INT,
    output_tokens INT,
    cached_input_tokens INT,
    reasoning_tokens INT,
    total_tokens INT,
    estimated_cost DOUBLE PRECISION,
    currency TEXT,
    native_cost DOUBLE PRECISION,
    native_currency TEXT,
    fx_rate DOUBLE PRECISION,
    fx_as_of TEXT,
    fx_source TEXT,
    price_version TEXT,
    pricing_snapshot JSONB,
    estimate_limitations TEXT,
    status TEXT,
    error_type TEXT,
    retry_count INT DEFAULT 0,
    usage_source TEXT,
    storage_backend TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_call_records (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_call_records (task_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_module ON llm_call_records (module_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_call_records (actual_model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_call_records (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_trace ON llm_call_records (trace_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_group ON llm_call_records (call_group_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load() -> List[Dict[str, Any]]:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _json_save(items: List[Dict[str, Any]]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(items[:_MAX_JSON], ensure_ascii=False, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(dir=str(STORE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STORE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def _get_engine() -> Optional[AsyncEngine]:
    global _engine, _session_factory, _pg_ready
    if _pg_ready is False:
        return None
    if _engine is not None:
        return _engine
    settings = get_settings()
    try:
        eng = create_async_engine(settings.database_url, pool_pre_ping=True)
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
        _engine = eng
        _session_factory = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        _pg_ready = True
        return _engine
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_usage Postgres unavailable, JSON fallback: %s", exc)
        _pg_ready = False
        _engine = None
        _session_factory = None
        return None


async def ensure_schema() -> bool:
    """确保表存在；成功返回 True。"""
    global _schema_ensured
    eng = await _get_engine()
    if eng is None:
        return False
    if _schema_ensured:
        return True
    try:
        async with eng.begin() as conn:
            for stmt in DDL.strip().split(";"):
                s = stmt.strip()
                if s:
                    await conn.execute(text(s))
        _schema_ensured = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_usage ensure_schema failed: %s", exc)
        return False


def reset_store_state_for_tests() -> None:
    """测试用：重置连接状态。"""
    global _engine, _session_factory, _pg_ready, _schema_ensured
    _engine = None
    _session_factory = None
    _pg_ready = None
    _schema_ensured = False


def storage_mode() -> str:
    if _pg_ready is True:
        return "postgres"
    if _pg_ready is False:
        return "json"
    return "unknown"


async def insert_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """写入一条记录。返回 record，并标注 storage_backend。

    始终写入 JSON（本地权威兜底），Postgres 可用时额外双写。
    避免「写入 JSON、查询空 Postgres」导致页面调用次数为 0。
    """
    rid = record.get("id")
    if not rid:
        raise ValueError("record.id required")

    # 去重：同 id 不重复写
    if await get_record(str(rid)) is not None:
        record["storage_backend"] = storage_mode()
        record["_deduped"] = True
        return record

    backends: List[str] = []
    _insert_json(record)
    backends.append("json")

    ok = await ensure_schema()
    if ok and _session_factory is not None:
        try:
            await _insert_pg(record)
            backends.append("postgres")
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_usage PG insert failed (JSON already saved): %s", exc)

    record["storage_backend"] = "+".join(backends) if len(backends) > 1 else backends[0]
    return record


async def _insert_pg(record: Dict[str, Any]) -> None:
    assert _session_factory is not None
    cols = [
        "id", "call_group_id", "attempt_index", "trace_id", "task_id", "conversation_id",
        "module_name", "node_name", "provider", "requested_model", "actual_model", "call_type",
        "started_at", "finished_at", "latency_ms", "time_to_first_token_ms",
        "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens",
        "estimated_cost", "currency", "native_cost", "native_currency",
        "fx_rate", "fx_as_of", "fx_source", "price_version", "pricing_snapshot",
        "estimate_limitations", "status", "error_type", "retry_count", "usage_source",
        "storage_backend", "created_at",
    ]
    payload = {c: record.get(c) for c in cols}
    payload["storage_backend"] = "postgres"
    if not payload.get("created_at"):
        payload["created_at"] = _now()
    # JSONB
    snap = payload.get("pricing_snapshot")
    if isinstance(snap, dict):
        payload["pricing_snapshot"] = json.dumps(snap, ensure_ascii=False, default=str)

    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    sql = text(
        f"INSERT INTO llm_call_records ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO NOTHING"
    )
    async with _session_factory() as session:
        async with session.begin():
            await session.execute(sql, payload)


def _insert_json(record: Dict[str, Any]) -> None:
    with _LOCK:
        items = _json_load()
        if any(it.get("id") == record.get("id") for it in items):
            return
        row = dict(record)
        row["storage_backend"] = "json"
        row.setdefault("created_at", _now())
        items.insert(0, row)
        _json_save(items)


async def get_record(record_id: str) -> Optional[Dict[str, Any]]:
    found: Optional[Dict[str, Any]] = None
    ok = await ensure_schema()
    if ok and _session_factory is not None:
        try:
            async with _session_factory() as session:
                res = await session.execute(
                    text("SELECT * FROM llm_call_records WHERE id = :id"),
                    {"id": record_id},
                )
                row = res.mappings().first()
                if row:
                    found = dict(row)
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_record pg failed: %s", exc)
    with _LOCK:
        for it in _json_load():
            if it.get("id") == record_id:
                if found is None:
                    return dict(it)
                # 两边都有则合并择新
                return _merge_by_id([found], [dict(it)])[0]
    return found


def _parse_dt(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _match_filters(
    it: Dict[str, Any],
    *,
    since: Optional[datetime],
    until: Optional[datetime],
    provider: str,
    model: str,
    module_name: str,
    task_id: str,
    status: str,
    call_group_id: str,
) -> bool:
    created = _parse_dt(it.get("created_at") or it.get("started_at"))
    if since and created and created < since:
        return False
    if until and created and created > until:
        return False
    if provider and (it.get("provider") or "") != provider:
        return False
    if model:
        am = (it.get("actual_model") or it.get("requested_model") or "")
        if model not in am and am != model:
            return False
    if module_name and (it.get("module_name") or "") != module_name:
        return False
    if task_id and (it.get("task_id") or "") != task_id:
        return False
    if status and (it.get("status") or "") != status:
        return False
    if call_group_id and (it.get("call_group_id") or "") != call_group_id:
        return False
    return True


def _merge_by_id(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 id 合并多数据源；同 id 保留 created_at 更新的一条。"""
    best: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for it in group:
            rid = str(it.get("id") or "")
            if not rid:
                continue
            prev = best.get(rid)
            if prev is None:
                best[rid] = it
                continue
            t_new = _parse_dt(it.get("created_at") or it.get("started_at"))
            t_old = _parse_dt(prev.get("created_at") or prev.get("started_at"))
            if t_new and t_old and t_new >= t_old:
                best[rid] = it
            elif t_new and not t_old:
                best[rid] = it
    items = list(best.values())

    def _sort_key(row: Dict[str, Any]) -> str:
        return str(row.get("created_at") or row.get("started_at") or "")

    items.sort(key=_sort_key, reverse=True)
    return items


async def _load_json_filtered(
    *,
    since: Optional[datetime],
    until: Optional[datetime],
    provider: str,
    model: str,
    module_name: str,
    task_id: str,
    status: str,
    call_group_id: str,
) -> List[Dict[str, Any]]:
    with _LOCK:
        return [
            it
            for it in _json_load()
            if _match_filters(
                it,
                since=since,
                until=until,
                provider=provider,
                model=model,
                module_name=module_name,
                task_id=task_id,
                status=status,
                call_group_id=call_group_id,
            )
        ]


async def query_records(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    task_id: str = "",
    status: str = "",
    call_group_id: str = "",
    offset: int = 0,
    limit: int = 50,
) -> Tuple[List[Dict[str, Any]], int, str]:
    """返回 (items, total, storage_backend_used)。

    合并 Postgres + JSON，避免双写分裂导致页面看到 0 条。
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    json_items = await _load_json_filtered(
        since=since,
        until=until,
        provider=provider,
        model=model,
        module_name=module_name,
        task_id=task_id,
        status=status,
        call_group_id=call_group_id,
    )
    pg_items: List[Dict[str, Any]] = []
    used = "json"

    ok = await ensure_schema()
    if ok and _session_factory is not None:
        try:
            # 拉较大窗口再与 JSON 合并后分页
            pg_items, _, _ = await _query_pg(
                since=since,
                until=until,
                provider=provider,
                model=model,
                module_name=module_name,
                task_id=task_id,
                status=status,
                call_group_id=call_group_id,
                offset=0,
                limit=min(5000, max(limit + offset, 500)),
            )
            used = "postgres+json" if json_items else "postgres"
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_usage query pg failed, JSON only: %s", exc)
            used = "json"

    if pg_items and json_items:
        items = _merge_by_id(pg_items, json_items)
        used = "postgres+json"
    elif pg_items:
        items = pg_items
    else:
        items = json_items
        used = "json"

    total = len(items)
    return items[offset : offset + limit], total, used


async def _query_pg(
    *,
    since: Optional[datetime],
    until: Optional[datetime],
    provider: str,
    model: str,
    module_name: str,
    task_id: str,
    status: str,
    call_group_id: str,
    offset: int,
    limit: int,
) -> Tuple[List[Dict[str, Any]], int, str]:
    assert _session_factory is not None
    wheres = ["1=1"]
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if since:
        wheres.append("created_at >= :since")
        params["since"] = since
    if until:
        wheres.append("created_at <= :until")
        params["until"] = until
    if provider:
        wheres.append("provider = :provider")
        params["provider"] = provider
    if model:
        wheres.append("(actual_model ILIKE :model OR requested_model ILIKE :model)")
        params["model"] = f"%{model}%"
    if module_name:
        wheres.append("module_name = :module_name")
        params["module_name"] = module_name
    if task_id:
        wheres.append("task_id = :task_id")
        params["task_id"] = task_id
    if status:
        wheres.append("status = :status")
        params["status"] = status
    if call_group_id:
        wheres.append("call_group_id = :call_group_id")
        params["call_group_id"] = call_group_id
    where_sql = " AND ".join(wheres)
    async with _session_factory() as session:
        cres = await session.execute(
            text(f"SELECT COUNT(*) AS c FROM llm_call_records WHERE {where_sql}"),
            params,
        )
        total = int(cres.scalar() or 0)
        res = await session.execute(
            text(
                f"SELECT * FROM llm_call_records WHERE {where_sql} "
                f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
        rows = [dict(r) for r in res.mappings().all()]
    return rows, total, "postgres"


async def fetch_all_for_agg(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    task_id: str = "",
    status: str = "",
    max_rows: int = 10000,
) -> Tuple[List[Dict[str, Any]], str]:
    items, _, backend = await query_records(
        since=since,
        until=until,
        provider=provider,
        model=model,
        module_name=module_name,
        task_id=task_id,
        status=status,
        offset=0,
        limit=min(max_rows, 200),
    )
    # 分页拉齐（简单实现，上限 max_rows）
    all_items = list(items)
    offset = 200
    while len(all_items) < max_rows:
        batch, total, backend = await query_records(
            since=since,
            until=until,
            provider=provider,
            model=model,
            module_name=module_name,
            task_id=task_id,
            status=status,
            offset=offset,
            limit=200,
        )
        if not batch:
            break
        all_items.extend(batch)
        offset += 200
        if offset >= total:
            break
    return all_items[:max_rows], backend
