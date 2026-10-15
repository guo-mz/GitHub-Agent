"""Testing Agent：无安全执行器强制 not_run/unavailable；禁止宿主机假沙箱。"""

from app.agents.testing_agent import run_testing
from app.services.sandbox_service import sandbox_status


def test_no_sandbox_forces_not_run(monkeypatch):
    monkeypatch.setattr(
        "app.agents.testing_agent.sandbox_status",
        lambda: {
            "can_execute": False,
            "mode": "unavailable",
            "docker": {"available": False, "reason": "docker_daemon_unavailable"},
            "status": "unavailable",
        },
    )
    env = run_testing(task_id="t1", repo="o/r", workspace_root="")
    assert env.status == "not_run"
    out = env.structured_output or {}
    assert out.get("not_run") is True
    assert out.get("unavailable") is True
    assert out.get("passed") is False
    assert out.get("failed") is False


def test_even_docker_probe_true_still_not_run(monkeypatch):
    """Docker daemon 可用也不等于可执行；禁止宿主机 subprocess。"""
    monkeypatch.setattr(
        "app.agents.testing_agent.sandbox_status",
        lambda: {
            "can_execute": False,
            "mode": "unavailable",
            "docker": {"available": True, "reason": "daemon_up_but_no_secure_executor"},
            "status": "unavailable",
        },
    )
    env = run_testing(
        task_id="t2",
        repo="o/r",
        requested_commands=["python -m pytest --collect-only", "rm -rf /"],
    )
    assert env.status == "not_run"
    assert (env.structured_output or {}).get("not_run") is True
    assert (env.structured_output or {}).get("passed") is False


def test_sandbox_status_never_allows_host_exec():
    st = sandbox_status()
    assert st.get("can_execute") is False
    assert st.get("mode") in {"unavailable", "none"}
