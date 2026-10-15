"""Gateway 路径分流单测。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.gateway.routing import resolve_upstream


def test_agent_routes():
    assert resolve_upstream("/api/chat/stream") == "agent"
    assert resolve_upstream("/api/approval/abc") == "agent"
    assert resolve_upstream("/api/chat") == "agent"
    assert resolve_upstream("/api/eval/run") == "agent"
    assert resolve_upstream("/api/llm-usage/summary") == "agent"


def test_repo_routes():
    assert resolve_upstream("/api/repos/o/r/tree") == "repo"
    assert resolve_upstream("/api/auth/github") == "repo"
    assert resolve_upstream("/api/webhook/github") == "repo"
    assert resolve_upstream("/api/github/setup") == "repo"


def test_health_not_proxied():
    assert resolve_upstream("/health") is None


def test_unknown():
    assert resolve_upstream("/api/unknown") is None
