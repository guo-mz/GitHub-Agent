#!/usr/bin/env python3
"""真实 OS 进程重启级 HITL 恢复验证（非单测内模拟）。

用法（需 Redis Stack 已启动，且 AsyncRedisSaver 可用）：

  # 一键：phase1 子进程 interrupt 后退出 → phase2 新进程 resume
  python scripts/verify_hitl_restart.py run

  # 或分步：
  python scripts/verify_hitl_restart.py phase1 --state-file /tmp/hitl_state.json
  python scripts/verify_hitl_restart.py phase2 --state-file /tmp/hitl_state.json

成功退出码 0；失败非 0。不调用 GitHub、不创建 PR。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, TypedDict

# 保证可 import app.*
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class HitlState(TypedDict):
    phase: str
    decision: str
    value: str
    pid: int


def _build_graph(checkpointer: Any):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def before(state: HitlState) -> Dict[str, Any]:
        return {
            "phase": "before",
            "value": state.get("value") or "v1",
            "pid": os.getpid(),
        }

    def wait(state: HitlState) -> Dict[str, Any]:
        decision = interrupt(
            {
                "type": "need_approval",
                "value": state.get("value"),
                "paused_pid": os.getpid(),
            }
        )
        if isinstance(decision, str):
            decision = {"decision": decision}
        return {
            "phase": "resumed",
            "decision": (decision or {}).get("decision") or "reject",
            "pid": os.getpid(),
        }

    def after(state: HitlState) -> Dict[str, Any]:
        return {
            "phase": "done",
            "value": f"{state.get('value')}:{state.get('decision')}:pid{os.getpid()}",
            "pid": os.getpid(),
        }

    g = StateGraph(HitlState)
    g.add_node("before", before)
    g.add_node("wait", wait)
    g.add_node("after", after)
    g.add_edge(START, "before")
    g.add_edge("before", "wait")
    g.add_edge("wait", "after")
    g.add_edge("after", END)
    return g.compile(checkpointer=checkpointer)


async def _get_redis_saver():
    from app.config import get_settings
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    url = (os.environ.get("REDIS_URL") or get_settings().redis_url or "").strip()
    saver = AsyncRedisSaver(redis_url=url)
    await saver.asetup()
    return saver, url


async def phase1(state_file: Path) -> int:
    """在本进程 interrupt 暂停，把 thread_id 写入 state_file 后退出（由调用方结束进程）。"""
    saver, url = await _get_redis_saver()
    thread_id = f"restart-hitl-{uuid.uuid4().hex}"
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10}
    graph = _build_graph(saver)
    await graph.ainvoke(
        {"phase": "", "decision": "", "value": "restart-demo", "pid": 0},
        config=config,
    )
    st = await graph.aget_state(config)
    if not st.next:
        print("ERROR: expected interrupt pause, got terminal state", file=sys.stderr)
        return 2
    payload = {
        "thread_id": thread_id,
        "redis_url": url,
        "next": list(st.next or ()),
        "phase1_pid": os.getpid(),
        "values": {
            "phase": (st.values or {}).get("phase"),
            "value": (st.values or {}).get("value"),
        },
    }
    state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "phase": 1, **payload}, ensure_ascii=False))
    return 0


async def phase2(state_file: Path) -> int:
    """全新进程：读取 thread_id，从 Redis checkpoint resume。"""
    from langgraph.types import Command

    if not state_file.exists():
        print(f"ERROR: missing state file {state_file}", file=sys.stderr)
        return 2
    meta = json.loads(state_file.read_text(encoding="utf-8"))
    thread_id = meta["thread_id"]
    phase1_pid = int(meta.get("phase1_pid") or 0)
    if phase1_pid == os.getpid():
        print(
            "ERROR: phase2 running in same PID as phase1 — not a real process restart",
            file=sys.stderr,
        )
        return 3

    saver, url = await _get_redis_saver()
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10}
    graph = _build_graph(saver)
    st = await graph.aget_state(config)
    if not st.next:
        print(
            f"ERROR: no checkpoint for thread_id={thread_id} (mode may be memory?)",
            file=sys.stderr,
        )
        return 4

    out = await graph.ainvoke(Command(resume={"decision": "approve"}), config=config)
    ok = (
        out.get("phase") == "done"
        and out.get("decision") == "approve"
        and "restart-demo" in (out.get("value") or "")
    )
    result = {
        "ok": ok,
        "phase": 2,
        "thread_id": thread_id,
        "phase1_pid": phase1_pid,
        "phase2_pid": os.getpid(),
        "different_process": phase1_pid != os.getpid(),
        "redis_url": url,
        "output": out,
        "remaining_next": list((await graph.aget_state(config)).next or ()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 5


def run_both() -> int:
    """父进程串联：子进程 phase1 → 子进程退出 → 子进程 phase2。"""
    state_path = Path(tempfile.gettempdir()) / f"hitl_restart_{uuid.uuid4().hex}.json"
    py = sys.executable
    script = str(Path(__file__).resolve())
    env = os.environ.copy()
    env.setdefault("REDIS_URL", "redis://localhost:6379/0")

    print("--- phase1 subprocess (interrupt then exit) ---")
    p1 = subprocess.run(
        [py, script, "phase1", "--state-file", str(state_path)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    print(p1.stdout)
    if p1.returncode != 0:
        print(p1.stderr, file=sys.stderr)
        return p1.returncode

    print("--- phase2 subprocess (new PID, resume from Redis) ---")
    p2 = subprocess.run(
        [py, script, "phase2", "--state-file", str(state_path)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    print(p2.stdout)
    if p2.stderr:
        print(p2.stderr, file=sys.stderr)
    try:
        state_path.unlink(missing_ok=True)
    except OSError:
        pass
    return p2.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="HITL Redis checkpoint process-restart verify")
    parser.add_argument("command", choices=["run", "phase1", "phase2"])
    parser.add_argument(
        "--state-file",
        default=str(Path(tempfile.gettempdir()) / "hitl_restart_state.json"),
    )
    args = parser.parse_args()
    state_file = Path(args.state_file)

    if args.command == "run":
        return run_both()
    if args.command == "phase1":
        return asyncio.run(phase1(state_file))
    return asyncio.run(phase2(state_file))


if __name__ == "__main__":
    raise SystemExit(main())
