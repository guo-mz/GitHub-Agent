"""隔离执行能力探测 —— 明确区分「Docker CLI/daemon 存在」与「可安全跑仓库测试」。

本阶段不提供宿主机 subprocess「假沙箱」；can_execute 恒为 False，
直到实现真正的无宿主机凭据、最小权限容器执行器。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)


def docker_daemon_available(timeout_sec: float = 3.0) -> Dict[str, Any]:
    """探测 Docker CLI + daemon。仅作运维可见性，不授权执行。"""
    docker = shutil.which("docker")
    if not docker:
        return {"available": False, "reason": "docker_cli_missing", "mode": "none"}
    try:
        proc = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:300]
            return {
                "available": False,
                "reason": "docker_daemon_unavailable",
                "detail": err or f"exit={proc.returncode}",
                "mode": "none",
            }
        ver = (proc.stdout or "").strip()
        return {
            "available": True,
            "reason": "daemon_up_but_no_secure_executor",
            "mode": "docker_probe_only",
            "version": ver,
        }
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "docker_probe_timeout", "mode": "none"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("docker probe failed: %s", exc)
        return {"available": False, "reason": str(exc)[:200], "mode": "none"}


def sandbox_status() -> Dict[str, Any]:
    """安全执行器状态。

    重要：即使 docker daemon 可用，本阶段 can_execute=False。
    禁止把宿主机 subprocess.run 当作沙箱。
    """
    d = docker_daemon_available()
    return {
        "can_execute": False,
        "mode": "unavailable",
        "docker": d,
        "policy": "host_subprocess_forbidden; secure_container_executor_not_implemented",
        "status": "unavailable",
    }
