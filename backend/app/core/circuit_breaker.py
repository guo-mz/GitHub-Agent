"""简单熔断器（Closed → Open → HalfOpen）—— Gateway 打上游用。

面试点：失败计数 / 冷却时间 / 半开试探；不是 Istio，但是同构语义。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    state: str = "closed"  # closed | open | half_open
    success_in_half: int = 0


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 20.0
    half_open_success: int = 1
    _states: Dict[str, BreakerState] = field(default_factory=dict)

    def _s(self, name: str) -> BreakerState:
        if name not in self._states:
            self._states[name] = BreakerState()
        return self._states[name]

    def allow(self, name: str) -> bool:
        st = self._s(name)
        if st.state == "closed":
            return True
        if st.state == "open":
            if time.time() - st.opened_at >= self.recovery_timeout:
                st.state = "half_open"
                st.success_in_half = 0
                return True
            return False
        # half_open：放行一次试探
        return True

    def record_success(self, name: str) -> None:
        st = self._s(name)
        if st.state == "half_open":
            st.success_in_half += 1
            if st.success_in_half >= self.half_open_success:
                st.state = "closed"
                st.failures = 0
                st.opened_at = 0.0
        else:
            st.failures = 0
            st.state = "closed"

    def record_failure(self, name: str) -> None:
        st = self._s(name)
        st.failures += 1
        if st.state == "half_open" or st.failures >= self.failure_threshold:
            st.state = "open"
            st.opened_at = time.time()

    def snapshot(self) -> Dict[str, dict]:
        return {
            k: {"state": v.state, "failures": v.failures, "opened_at": v.opened_at}
            for k, v in self._states.items()
        }


# Gateway 单例
gateway_breaker = CircuitBreaker()
