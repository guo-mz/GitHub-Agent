"""轻量服务发现：Redis Hash + TTL 心跳（面试可讲，无需 Consul/Nacos）。

键：svc:reg:{name} → {url, ts, meta}
Gateway 优先从注册表解析上游，失败回退 .env 静态 URL。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

_KEY = "svc:reg"
_client: Optional[aioredis.Redis] = None
_heartbeat_task = None


def _registry_redis_url() -> str:
    s = get_settings()
    return (s.registry_redis_url or s.redis_url or "redis://localhost:6379/0").strip()


async def _redis() -> Optional[aioredis.Redis]:
    global _client
    if _client is not None:
        return _client
    try:
        _client = aioredis.from_url(_registry_redis_url(), decode_responses=True)
        await _client.ping()
        return _client
    except Exception as exc:  # noqa: BLE001
        logger.warning("service registry redis unavailable: %s", exc)
        _client = None
        return None


async def register(name: str, url: str, *, meta: Optional[Dict[str, Any]] = None, ttl: int = 30) -> bool:
    r = await _redis()
    if r is None:
        return False
    payload = {
        "url": url.rstrip("/"),
        "ts": time.time(),
        "ttl": ttl,
        "meta": meta or {},
    }
    await r.hset(_KEY, name, json.dumps(payload, ensure_ascii=False))
    return True


async def heartbeat(name: str, url: str, *, meta: Optional[Dict[str, Any]] = None, ttl: int = 30) -> None:
    await register(name, url, meta=meta, ttl=ttl)


async def deregister(name: str) -> None:
    r = await _redis()
    if r is None:
        return
    await r.hdel(_KEY, name)


async def resolve(name: str, *, max_age: float = 45.0) -> Optional[str]:
    """返回仍存活的实例 URL；过期则视为下线。"""
    r = await _redis()
    if r is None:
        return None
    raw = await r.hget(_KEY, name)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    ts = float(data.get("ts") or 0)
    if time.time() - ts > max_age:
        return None
    url = (data.get("url") or "").rstrip("/")
    return url or None


async def list_services() -> Dict[str, Dict[str, Any]]:
    r = await _redis()
    if r is None:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    raw = await r.hgetall(_KEY)
    now = time.time()
    for name, blob in (raw or {}).items():
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        age = now - float(data.get("ts") or 0)
        out[name] = {
            "url": data.get("url"),
            "age_sec": round(age, 1),
            "alive": age <= float(data.get("ttl") or 30) * 1.5,
            "meta": data.get("meta") or {},
        }
    return out


async def start_heartbeat_loop(name: str, url: str, *, interval: float = 10.0, meta: Optional[Dict[str, Any]] = None):
    """后台心跳；在 FastAPI lifespan 里 create_task。"""
    import asyncio

    async def _loop():
        while True:
            try:
                await heartbeat(name, url, meta=meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("heartbeat %s failed: %s", name, exc)
            await asyncio.sleep(interval)

    await heartbeat(name, url, meta=meta)
    return asyncio.create_task(_loop())
