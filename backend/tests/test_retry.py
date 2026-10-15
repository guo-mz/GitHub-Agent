"""重试兜底单测：可重试判定 + 指数退避执行。"""

import asyncio

from app.core.retry import is_retryable, with_retry


class _HttpExc(Exception):
    def __init__(self, status_code: int, msg: str = ""):
        super().__init__(msg or f"HTTP {status_code}")
        self.status_code = status_code


def test_retryable_timeout_and_5xx():
    assert is_retryable(TimeoutError("timed out"))
    assert is_retryable(ConnectionError("reset"))
    assert is_retryable(_HttpExc(429, "rate limit"))
    assert is_retryable(_HttpExc(503))
    assert is_retryable(RuntimeError("upstream overloaded, try again"))


def test_not_retryable_auth_and_4xx():
    assert not is_retryable(_HttpExc(401, "unauthorized"))
    assert not is_retryable(_HttpExc(400, "bad request"))
    assert not is_retryable(ValueError("invalid json"))


def test_with_retry_succeeds_after_transient():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HttpExc(503, "unavailable")
        return "ok"

    out = asyncio.run(
        with_retry(flaky, attempts=3, base_delay=0.01, max_delay=0.02, label="t")
    )
    assert out == "ok"
    assert calls["n"] == 3


def test_with_retry_stops_on_permanent():
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise _HttpExc(401, "nope")

    try:
        asyncio.run(with_retry(bad, attempts=5, base_delay=0.01, label="auth"))
        assert False, "expected raise"
    except _HttpExc:
        pass
    assert calls["n"] == 1


def test_on_retry_callback():
    seen = []

    async def once_fail():
        if not seen:
            raise TimeoutError("x")
        return 1

    def on_retry(attempt, exc, delay):
        seen.append((attempt, type(exc).__name__, delay > 0))

    assert (
        asyncio.run(
            with_retry(once_fail, attempts=2, base_delay=0.01, on_retry=on_retry)
        )
        == 1
    )
    assert seen and seen[0][0] == 1
