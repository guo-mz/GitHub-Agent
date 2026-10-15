"""网关熔断器状态机单测。"""

from app.core.circuit_breaker import CircuitBreaker


def test_opens_after_threshold():
    b = CircuitBreaker(failure_threshold=3, recovery_timeout=60)
    assert b.allow("agent")
    b.record_failure("agent")
    b.record_failure("agent")
    assert b.allow("agent")
    b.record_failure("agent")
    assert not b.allow("agent")
    assert b.snapshot()["agent"]["state"] == "open"


def test_half_open_then_close():
    b = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0, half_open_success=1)
    b.record_failure("repo")
    assert b.snapshot()["repo"]["state"] == "open"
    assert b.allow("repo")  # recovery_timeout=0 → half_open
    b.record_success("repo")
    assert b.snapshot()["repo"]["state"] == "closed"
