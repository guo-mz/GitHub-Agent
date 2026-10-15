"""Testing Agent —— 无安全隔离执行器时强制 unavailable/not_run。

禁止宿主机 subprocess 执行仓库命令；禁止用 LLM 文本判定通过。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agents.contracts import AgentEnvelope, CheckResult, utc_now
from app.services.sandbox_service import sandbox_status

logger = logging.getLogger(__name__)


def run_testing(
    *,
    task_id: str,
    repo: str,
    workspace_root: str = "",
    requested_commands: Optional[List[str]] = None,
    content_hash: str = "",
) -> AgentEnvelope:
    started = utc_now()
    sand = sandbox_status()
    env = AgentEnvelope(
        task_id=task_id,
        agent_name="testing",
        status="running",
        input_context={
            "repo": repo,
            "sandbox": sand,
            "content_hash": content_hash,
            "requested_commands": list(requested_commands or [])[:5],
            "workspace_root": workspace_root[:240] if workspace_root else "",
        },
        started_at=started,
    )

    # 本阶段：无安全执行器 → 一律 unavailable / not_run（即使 Docker daemon 在线）
    check = CheckResult(
        name="secure_executor_gate",
        status="not_run",
        log=(
            "无可靠隔离执行环境：禁止在宿主机执行测试命令。"
            f" sandbox={sand.get('mode')}; docker={sand.get('docker', {}).get('reason')}"
        ),
        command="",
    )
    env.status = "not_run"
    env.structured_output = {
        "checks": [check.model_dump()],
        "summary": "testing_unavailable_no_secure_executor",
        "passed": False,
        "failed": False,
        "not_run": True,
        "unavailable": True,
        "content_hash": content_hash,
    }
    env.artifacts = {"sandbox": sand}
    env.finished_at = utc_now()
    return env
