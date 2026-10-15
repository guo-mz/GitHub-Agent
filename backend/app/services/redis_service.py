"""Redis 三重职责：① Checkpointer ② 令牌桶限流 ③ 幂等锁 + 会话缓存。

无 Redis 时自动降级为进程内内存（本地开发可启动；生产务必连真实 Redis）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Set

import redis.asyncio as aioredis

from app.config import get_settings
from app.core.idempotency import IdempotencyResult
from app.core.rate_limit import TOKEN_BUCKET_LUA, RateLimitResult

logger = logging.getLogger(__name__)


class _MemoryStore:
    """进程内兜底：kv / set / list / 令牌桶。"""

    def __init__(self) -> None:
        self.kv: Dict[str, Any] = {}
        self.expiry: Dict[str, float] = {}
        self.sets: Dict[str, Set[str]] = {}
        self.lists: Dict[str, List[str]] = {}
        self.buckets: Dict[str, Dict[str, float]] = {}

    def _alive(self, key: str) -> bool:
        exp = self.expiry.get(key)
        if exp is not None and time.time() > exp:
            self.kv.pop(key, None)
            self.expiry.pop(key, None)
            return False
        return key in self.kv

    def set(self, key: str, value: Any, *, nx: bool = False, px: Optional[int] = None, ex: Optional[int] = None) -> bool:
        if nx and self._alive(key):
            return False
        self.kv[key] = value
        if px:
            self.expiry[key] = time.time() + px / 1000.0
        elif ex:
            self.expiry[key] = time.time() + ex
        return True

    def get(self, key: str) -> Any:
        if not self._alive(key):
            return None
        return self.kv.get(key)

    def delete(self, *keys: str) -> None:
        for k in keys:
            self.kv.pop(k, None)
            self.expiry.pop(k, None)

    def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key: str, member: str) -> None:
        if key in self.sets:
            self.sets[key].discard(member)

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def llen(self, key: str) -> int:
        return len(self.lists.get(key, []))

    def expire(self, key: str, seconds: int) -> None:
        self.expiry[key] = time.time() + seconds

    def rate_limit(self, key: str, limit: int, window: int, cost: int) -> RateLimitResult:
        refill = limit / float(window)
        now = time.time()
        b = self.buckets.get(key)
        if not b:
            b = {"tokens": float(limit), "ts": now}
            self.buckets[key] = b
        elapsed = max(0.0, now - b["ts"])
        b["tokens"] = min(float(limit), b["tokens"] + elapsed * refill)
        b["ts"] = now
        if b["tokens"] >= cost:
            b["tokens"] -= cost
            return RateLimitResult(True, b["tokens"], 0)
        retry = int((cost - b["tokens"]) / refill) + 1
        return RateLimitResult(False, b["tokens"], retry)


class RedisService:
    def __init__(self) -> None:
        self._client: Optional[aioredis.Redis] = None
        self._lua_sha: Optional[str] = None
        self._memory = _MemoryStore()
        self.fallback = False

    async def connect(self, *, force: bool = False) -> None:
        """连接 Redis。force=True 时清除粘性 fallback 并重试（测试/运维探测用）。"""
        if force:
            try:
                await self.close()
            except Exception:  # noqa: BLE001
                self._client = None
            self.fallback = False
        if self._client is not None or self.fallback:
            return
        settings = get_settings()
        try:
            client = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await client.ping()
            self._client = client
            self._lua_sha = await self._client.script_load(TOKEN_BUCKET_LUA)
            self.fallback = False
            logger.info("Redis connected: %s", settings.redis_url)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            self.fallback = True
            logger.warning("Redis unavailable (%s), using in-memory fallback", exc)

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.aclose()
            except RuntimeError as exc:
                # 跨 asyncio.run() 边界关闭时，旧 loop 可能已关闭
                if "Event loop is closed" not in str(exc):
                    raise
            finally:
                self._client = None

    async def reset_for_tests(self) -> None:
        """测试隔离：关闭连接并清除 fallback，便于重新探测。"""
        try:
            await self.close()
        except Exception:  # noqa: BLE001
            self._client = None
        self.fallback = False
        self._lua_sha = None
        self._memory = _MemoryStore()

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("RedisService not connected")
        return self._client

    async def ping(self) -> bool:
        try:
            await self.connect()
            if self.fallback:
                return False
            return bool(await self.client.ping())
        except Exception:  # noqa: BLE001
            return False

    async def get_checkpointer(self):
        from app.core.checkpointer import get_checkpointer

        return await get_checkpointer()

    async def acquire_idempotency(
        self,
        key: str,
        ttl: int = 300,
        value: str = "1",
    ) -> IdempotencyResult:
        await self.connect()
        if self.fallback:
            ok = self._memory.set(key, value, nx=True, px=ttl * 1000)
            if ok:
                return IdempotencyResult(acquired=True)
            return IdempotencyResult(acquired=False, cached_value=self._memory.get(key))
        ok = await self.client.set(key, value, nx=True, px=ttl * 1000)
        if ok:
            return IdempotencyResult(acquired=True)
        cached = await self.client.get(key)
        return IdempotencyResult(acquired=False, cached_value=cached)

    async def set_idempotency_result(self, key: str, result: str, ttl: int = 300) -> None:
        await self.connect()
        if self.fallback:
            self._memory.set(key, result, px=ttl * 1000)
            return
        await self.client.set(key, result, px=ttl * 1000)

    async def release_idempotency(self, key: str) -> None:
        await self.connect()
        if self.fallback:
            self._memory.delete(key)
            return
        await self.client.delete(key)

    async def rate_limit(
        self,
        key: str,
        limit: int,
        window: int,
        cost: int = 1,
    ) -> RateLimitResult:
        await self.connect()
        if self.fallback:
            return self._memory.rate_limit(f"rl:{key}", limit, window, cost)

        refill = limit / float(window)
        now = time.time()
        namespaced = f"rl:{key}"
        try:
            if self._lua_sha:
                res = await self.client.evalsha(
                    self._lua_sha, 1, namespaced, limit, refill, now, cost
                )
            else:
                res = await self.client.eval(
                    TOKEN_BUCKET_LUA, 1, namespaced, limit, refill, now, cost
                )
        except Exception:  # noqa: BLE001
            res = await self.client.eval(
                TOKEN_BUCKET_LUA, 1, namespaced, limit, refill, now, cost
            )
            self._lua_sha = await self.client.script_load(TOKEN_BUCKET_LUA)

        return RateLimitResult(int(res[0]) == 1, float(res[1]), int(res[2]))

    async def cache_session(self, tid: str, data: Dict[str, Any], ttl: int = 86400) -> None:
        await self.connect()
        payload = json.dumps(data, default=str)
        if self.fallback:
            self._memory.set(f"sess:{tid}", payload, ex=ttl)
            return
        await self.client.set(f"sess:{tid}", payload, ex=ttl)

    async def get_session(self, tid: str) -> Optional[Dict[str, Any]]:
        await self.connect()
        if self.fallback:
            raw = self._memory.get(f"sess:{tid}")
        else:
            raw = await self.client.get(f"sess:{tid}")
        if not raw:
            return None
        return json.loads(raw)

    async def save_pending_approval(self, tid: str, payload: Dict[str, Any], ttl: int = 3600) -> None:
        await self.connect()
        raw = json.dumps(payload, default=str)
        if self.fallback:
            self._memory.set(f"approval:{tid}", raw, ex=ttl)
            self._memory.sadd("approval:pending", tid)
        else:
            await self.client.set(f"approval:{tid}", raw, ex=ttl)
            await self.client.sadd("approval:pending", tid)
        # 磁盘双写：无 Redis / 进程重启后仍能校验
        try:
            from app.services import approval_store

            approval_store.save_pending(tid, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval_store.save_pending failed: %s", exc)

    async def get_pending_approval(self, tid: str) -> Optional[Dict[str, Any]]:
        await self.connect()
        if self.fallback:
            raw = self._memory.get(f"approval:{tid}")
        else:
            raw = await self.client.get(f"approval:{tid}")
        if raw:
            return json.loads(raw)
        # 热缓存未命中时回退磁盘（例如新进程）
        try:
            from app.services import approval_store

            return approval_store.get_pending(tid)
        except Exception:  # noqa: BLE001
            return None

    async def list_pending_approvals(self) -> list[Dict[str, Any]]:
        await self.connect()
        if self.fallback:
            tids = self._memory.smembers("approval:pending")
        else:
            tids = await self.client.smembers("approval:pending")
        items = []
        for tid in tids or []:
            data = await self.get_pending_approval(tid)
            if data:
                items.append(data)
            else:
                if self.fallback:
                    self._memory.srem("approval:pending", tid)
                else:
                    await self.client.srem("approval:pending", tid)
        return items

    async def resolve_approval(
        self,
        tid: str,
        *,
        decision: str = "",
        approval_kind: str = "",
        ttl: int = 86400,
    ) -> None:
        """关闭 pending，并写入 resolved 标记（内存 + 磁盘），阻止旧审批被重复使用。"""
        await self.connect()
        pending = None
        if self.fallback:
            raw_p = self._memory.get(f"approval:{tid}")
        else:
            raw_p = await self.client.get(f"approval:{tid}")
        if raw_p:
            try:
                pending = json.loads(raw_p)
            except Exception:  # noqa: BLE001
                pending = None
        if not pending:
            try:
                from app.services import approval_store

                pending = approval_store.get_pending(tid)
            except Exception:  # noqa: BLE001
                pending = None
        kind = approval_kind or (
            (pending or {}).get("approval_kind")
            or ((pending or {}).get("payload") or {}).get("approval_kind")
            or ""
        )
        resolved = {
            "thread_id": tid,
            "status": "resolved",
            "decision": decision or "resolved",
            "approval_kind": kind,
            "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        raw = json.dumps(resolved, default=str)
        if self.fallback:
            self._memory.delete(f"approval:{tid}")
            self._memory.srem("approval:pending", tid)
            self._memory.set(f"approval:resolved:{tid}", raw, ex=ttl)
        else:
            await self.client.delete(f"approval:{tid}")
            await self.client.srem("approval:pending", tid)
            await self.client.set(f"approval:resolved:{tid}", raw, ex=ttl)
        try:
            from app.services import approval_store

            approval_store.mark_resolved(tid, decision=decision or "resolved", approval_kind=kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval_store.mark_resolved failed: %s", exc)

    async def get_resolved_approval(self, tid: str) -> Optional[Dict[str, Any]]:
        await self.connect()
        key = f"approval:resolved:{tid}"
        if self.fallback:
            raw = self._memory.get(key)
        else:
            raw = await self.client.get(key)
        if raw:
            return json.loads(raw)
        try:
            from app.services import approval_store

            return approval_store.get_resolved(tid)
        except Exception:  # noqa: BLE001
            return None

    async def track_trajectory(self, tid: str, node: str, max_len: int = 50) -> int:
        await self.connect()
        key = f"traj:{tid}"
        if self.fallback:
            length = self._memory.rpush(key, node)
            self._memory.expire(key, 3600)
            return length
        await self.client.rpush(key, node)
        await self.client.expire(key, 3600)
        return int(await self.client.llen(key))

    async def is_circuit_open(self, tid: str, threshold: int = 40) -> bool:
        await self.connect()
        if self.fallback:
            return self._memory.llen(f"traj:{tid}") >= threshold
        length = await self.client.llen(f"traj:{tid}")
        return int(length or 0) >= threshold

    async def save_review_memory(self, repo: str, payload: Dict[str, Any], ttl: int = 86400) -> None:
        """缓存最近一次审查报告，供「按审查结果修复」续聊。"""
        await self.connect()
        key = f"review:{repo}"
        raw = json.dumps(payload, default=str, ensure_ascii=False)
        if self.fallback:
            self._memory.set(key, raw, ex=ttl)
            return
        await self.client.set(key, raw, ex=ttl)

    async def get_review_memory(self, repo: str) -> Optional[Dict[str, Any]]:
        await self.connect()
        key = f"review:{repo}"
        if self.fallback:
            raw = self._memory.get(key)
        else:
            raw = await self.client.get(key)
        if not raw:
            return None
        return json.loads(raw)


redis_service = RedisService()
