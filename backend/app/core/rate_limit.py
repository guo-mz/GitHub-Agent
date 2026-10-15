"""令牌桶限流（Lua 原子脚本）—— 保护 GitHub 5000/h 配额，超限自动降级。

面试点：为什么不用简单 INCR？
- INCR + EXPIRE 非原子，高并发会超发；Lua 在 Redis 单线程内原子执行。
"""

from __future__ import annotations

TOKEN_BUCKET_LUA = """
-- KEYS[1]=bucket key, ARGV[1]=capacity, ARGV[2]=refill_rate(per sec), ARGV[3]=now, ARGV[4]=cost
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)
ts = now

local allowed = 0
local retry_after = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_after = math.ceil((cost - tokens) / refill)
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', ts)
redis.call('EXPIRE', key, math.ceil(capacity / refill) + 60)
return {allowed, tokens, retry_after}
"""


class RateLimitResult:
    __slots__ = ("allowed", "remaining", "retry_after")

    def __init__(self, allowed: bool, remaining: float, retry_after: int):
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after = retry_after
