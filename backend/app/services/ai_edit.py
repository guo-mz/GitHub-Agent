"""在线编辑器：按自然语言指令改写当前缓冲区代码（支持 SSE 流式）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_utils import make_chat_llm
from app.services.llm_usage.context import llm_call_context
from app.services.llm_usage.recorder import ainvoke_tracked, astream_tracked, primary_provider_info

_TZ_SH = timezone(timedelta(hours=8))

EDIT_SYSTEM = """你是结对编程助手。根据用户指令改写给定源码。

系统当前时间：{now}（Asia/Shanghai）
用户说「今天 / 当前日期」时必须用上述日期（YYYY-MM-DD），禁止编造或沿用训练记忆里的日期。

规则：
1. 只输出完整修改后的源码，不要 Markdown 代码围栏，不要解释
2. 保持原文件风格与未要求改动的部分
3. 指令含糊时做最小合理改动
"""


def _now_str() -> str:
    return datetime.now(_TZ_SH).strftime("%Y-%m-%d %H:%M:%S %z")


def _chunk_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
        return "".join(parts)
    return str(content)


def strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _prompt(*, path: str, content: str, instruction: str) -> str:
    return (
        f"文件：{path}\n\n"
        f"## 指令\n{instruction}\n\n"
        f"## 当前内容\n```\n{content[:40000]}\n```\n"
    )


def _edit_system() -> str:
    return EDIT_SYSTEM.format(now=_now_str())


def _msgs(*, path: str, content: str, instruction: str) -> list:
    return [
        SystemMessage(content=_edit_system()),
        HumanMessage(content=_prompt(path=path, content=content, instruction=instruction)),
    ]


async def ai_edit_buffer(*, path: str, content: str, instruction: str) -> str:
    provider, model = primary_provider_info()
    llm = make_chat_llm(temperature=0.15, streaming=False)
    with llm_call_context(module_name="editor", node_name="ai_edit", label="ai_edit"):
        resp = await ainvoke_tracked(
            llm,
            _msgs(path=path, content=content, instruction=instruction),
            provider=provider,
            requested_model=model,
            label="ai_edit",
            call_type="chat",
        )
    return strip_code_fences(_chunk_text(resp.content))


async def ai_edit_buffer_stream(
    *,
    path: str,
    content: str,
    instruction: str,
) -> AsyncIterator[Dict[str, Any]]:
    """SSE 友好事件：delta → done（content 为去围栏后的完整源码）。"""
    yield {"event": "start", "data": {"path": path, "status": "streaming"}}
    acc = ""
    provider, model = primary_provider_info()
    llm = make_chat_llm(temperature=0.15, streaming=True, stream_usage=True)
    try:
        with llm_call_context(
            module_name="editor", node_name="ai_edit_stream", label="ai_edit_stream"
        ):
            async for chunk in astream_tracked(
                llm,
                _msgs(path=path, content=content, instruction=instruction),
                provider=provider,
                requested_model=model,
                label="ai_edit_stream",
            ):
                delta = _chunk_text(getattr(chunk, "content", None))
                if not delta:
                    continue
                acc += delta
                yield {"event": "delta", "data": {"delta": delta}}
        cleaned = strip_code_fences(acc)
        yield {
            "event": "done",
            "data": {
                "path": path,
                "content": cleaned,
                "detail": "AI 流式改写完成，请确认后保存上传",
            },
        }
    except Exception as exc:  # noqa: BLE001
        yield {"event": "error", "data": {"message": str(exc)}}
