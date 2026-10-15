"""python -m app.mcp_server  → stdio MCP（供 Cursor mcp.json 调用）。"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 保证从任意 cwd 可导入 app
_BACKEND = Path(__file__).resolve().parents[2]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

logging.basicConfig(level=logging.INFO, stream=sys.stderr)


def main() -> None:
    from app.mcp_server.server import run_stdio

    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
