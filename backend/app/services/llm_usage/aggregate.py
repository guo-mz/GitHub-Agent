"""汇总统计：总览 / 按模型 / 按模块 / 日趋势 / 任务明细。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.services.llm_usage import store as usage_store


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _sum_known(vals: List[Optional[int]]) -> Tuple[Optional[int], int, int]:
    """返回 (sum_or_None, known_count, unknown_count)。全未知则 sum=None。"""
    known = [v for v in vals if v is not None]
    unknown = len(vals) - len(known)
    if not known:
        return None, 0, unknown
    return sum(known), len(known), unknown


def _sum_cost(vals: List[Optional[float]]) -> Tuple[Optional[float], int, int]:
    known = [v for v in vals if v is not None]
    unknown = len(vals) - len(known)
    if not known:
        return None, 0, unknown
    return float(sum(known)), len(known), unknown


def serialize_record(r: Dict[str, Any]) -> Dict[str, Any]:
    """API 友好序列化；保留 None，不用 0 伪装。"""
    out = dict(r)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


async def overview(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
    task_id: str = "",
) -> Dict[str, Any]:
    rows, backend = await usage_store.fetch_all_for_agg(
        since=since,
        until=until,
        provider=provider,
        model=model,
        module_name=module_name,
        task_id=task_id,
    )
    n = len(rows)
    ok = sum(1 for r in rows if r.get("status") == "ok")
    failed = sum(1 for r in rows if r.get("status") not in {"ok", None})
    latencies = [_num(r.get("latency_ms")) for r in rows]
    lat_known = [x for x in latencies if x is not None]
    inp_sum, inp_k, inp_u = _sum_known([_int(r.get("input_tokens")) for r in rows])
    out_sum, out_k, out_u = _sum_known([_int(r.get("output_tokens")) for r in rows])
    tot_sum, tot_k, tot_u = _sum_known([_int(r.get("total_tokens")) for r in rows])
    cost_sum, cost_k, cost_u = _sum_cost([_num(r.get("estimated_cost")) for r in rows])
    return {
        "storage_backend": backend,
        "call_count": n,
        "success_count": ok,
        "failure_count": failed,
        "failure_rate": (failed / n) if n else None,
        "input_tokens": inp_sum,
        "input_tokens_known": inp_k,
        "input_tokens_unknown": inp_u,
        "output_tokens": out_sum,
        "output_tokens_known": out_k,
        "output_tokens_unknown": out_u,
        "total_tokens": tot_sum,
        "total_tokens_known": tot_k,
        "total_tokens_unknown": tot_u,
        "estimated_cost": cost_sum,
        "estimated_cost_known": cost_k,
        "estimated_cost_unknown": cost_u,
        "currency": "CNY",
        "avg_latency_ms": (sum(lat_known) / len(lat_known)) if lat_known else None,
        "cost_note": "估算费用（CNY），非服务商账单实付；未知价格不计入合计。",
    }


async def by_model(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    provider: str = "",
    module_name: str = "",
    task_id: str = "",
) -> Dict[str, Any]:
    rows, backend = await usage_store.fetch_all_for_agg(
        since=since, until=until, provider=provider, module_name=module_name, task_id=task_id
    )
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        key = (
            str(r.get("provider") or "unknown"),
            str(r.get("actual_model") or r.get("requested_model") or "unknown"),
        )
        groups.setdefault(key, []).append(r)

    items = []
    for (prov, mod), rs in groups.items():
        n = len(rs)
        failed = sum(1 for r in rs if r.get("status") != "ok")
        lat = [x for x in (_num(r.get("latency_ms")) for r in rs) if x is not None]
        tot, _, _ = _sum_known([_int(r.get("total_tokens")) for r in rs])
        cost, _, cost_u = _sum_cost([_num(r.get("estimated_cost")) for r in rs])
        actual_known = sum(1 for r in rs if r.get("actual_model"))
        items.append(
            {
                "provider": prov,
                "model": mod,
                "actual_model_known_count": actual_known,
                "call_count": n,
                "total_tokens": tot,
                "estimated_cost": cost,
                "estimated_cost_unknown": cost_u,
                "avg_latency_ms": (sum(lat) / len(lat)) if lat else None,
                "failure_rate": (failed / n) if n else None,
            }
        )
    items.sort(key=lambda x: x["call_count"], reverse=True)
    return {"storage_backend": backend, "currency": "CNY", "items": items}


async def by_module(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    provider: str = "",
    model: str = "",
    task_id: str = "",
) -> Dict[str, Any]:
    rows, backend = await usage_store.fetch_all_for_agg(
        since=since, until=until, provider=provider, model=model, task_id=task_id
    )
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(str(r.get("module_name") or "unknown"), []).append(r)

    items = []
    for mod, rs in groups.items():
        n = len(rs)
        lat = sorted(x for x in (_num(r.get("latency_ms")) for r in rs) if x is not None)
        inp, _, _ = _sum_known([_int(r.get("input_tokens")) for r in rs])
        out, _, _ = _sum_known([_int(r.get("output_tokens")) for r in rs])
        cost, _, cost_u = _sum_cost([_num(r.get("estimated_cost")) for r in rs])
        items.append(
            {
                "module_name": mod,
                "call_count": n,
                "input_tokens": inp,
                "output_tokens": out,
                "estimated_cost": cost,
                "estimated_cost_unknown": cost_u,
                "avg_latency_ms": (sum(lat) / len(lat)) if lat else None,
                "p95_latency_ms": _percentile(lat, 95),
            }
        )
    items.sort(key=lambda x: x["call_count"], reverse=True)
    return {"storage_backend": backend, "currency": "CNY", "items": items}


async def daily_trends(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    provider: str = "",
    model: str = "",
    module_name: str = "",
) -> Dict[str, Any]:
    rows, backend = await usage_store.fetch_all_for_agg(
        since=since,
        until=until,
        provider=provider,
        model=model,
        module_name=module_name,
    )
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        created = r.get("created_at") or r.get("started_at")
        if isinstance(created, datetime):
            day = created.astimezone(timezone.utc).strftime("%Y-%m-%d")
        elif isinstance(created, str) and len(created) >= 10:
            day = created[:10]
        else:
            day = "unknown"
        by_day.setdefault(day, []).append(r)

    items = []
    for day in sorted(by_day.keys()):
        rs = by_day[day]
        tot, _, _ = _sum_known([_int(r.get("total_tokens")) for r in rs])
        cost, _, cost_u = _sum_cost([_num(r.get("estimated_cost")) for r in rs])
        failed = sum(1 for r in rs if r.get("status") != "ok")
        items.append(
            {
                "date": day,
                "call_count": len(rs),
                "total_tokens": tot,
                "estimated_cost": cost,
                "estimated_cost_unknown": cost_u,
                "failure_count": failed,
            }
        )
    return {"storage_backend": backend, "currency": "CNY", "items": items}


async def task_detail(task_id: str) -> Dict[str, Any]:
    rows, backend = await usage_store.fetch_all_for_agg(task_id=task_id, max_rows=2000)
    rows_sorted = sorted(rows, key=lambda r: str(r.get("started_at") or ""))
    model_latency_sum = 0
    model_latency_known = 0
    for r in rows_sorted:
        lat = _num(r.get("latency_ms"))
        if lat is not None:
            model_latency_sum += lat
            model_latency_known += 1

    # 任务墙钟耗时：来自 task_run_service
    wall_ms = None
    from app.services import task_run_service as trs

    run = trs.get_run(task_id) or trs.get_run_by_thread(task_id)
    if run:
        started = run.get("started_at")
        ended = run.get("ended_at") or run.get("updated_at")
        try:
            if started and ended:
                s = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                e = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
                wall_ms = max(0, int((e - s).total_seconds() * 1000))
        except Exception:  # noqa: BLE001
            wall_ms = None

    cost, _, cost_u = _sum_cost([_num(r.get("estimated_cost")) for r in rows_sorted])
    tot, _, tot_u = _sum_known([_int(r.get("total_tokens")) for r in rows_sorted])
    return {
        "storage_backend": backend,
        "task_id": task_id,
        "task": trs.public_view(run) if run else None,
        "call_count": len(rows_sorted),
        "total_tokens": tot,
        "total_tokens_unknown": tot_u,
        "estimated_cost": cost,
        "estimated_cost_unknown": cost_u,
        "currency": "CNY",
        "model_latency_sum_ms": model_latency_sum if model_latency_known else None,
        "task_wall_clock_ms": wall_ms,
        "latency_note": (
            "model_latency_sum_ms 为各次模型调用耗时之和（并行时可能大于墙钟）；"
            "task_wall_clock_ms 为任务起止墙钟时间，二者不可互相替代。"
        ),
        "calls": [serialize_record(r) for r in rows_sorted],
        "cost_note": "估算费用，非账单实付。",
    }
