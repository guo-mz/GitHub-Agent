"""上下文工程：token 预算裁剪 + 工具输出截断 + 超长摘要。

面试点：
- Checkpointer 仍保留完整轨迹（可恢复 / 可审计）
- 进 LLM 前再组「工作上下文」：摘要 + 最近窗口，避免撑爆 context window
- 裁剪时保持 tool_calls ↔ ToolMessage 配对，防止 OpenAI 协议报错
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "[context_summary]"


@dataclass
class ContextMeta:
    raw_messages: int = 0
    kept_messages: int = 0
    dropped_messages: int = 0
    approx_tokens_before: int = 0
    approx_tokens_after: int = 0
    tool_truncated: int = 0
    summarized: bool = False
    mode: str = "passthrough"  # passthrough | trim | summarize+trim

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw_messages": self.raw_messages,
            "kept_messages": self.kept_messages,
            "dropped_messages": self.dropped_messages,
            "approx_tokens_before": self.approx_tokens_before,
            "approx_tokens_after": self.approx_tokens_after,
            "tool_truncated": self.tool_truncated,
            "summarized": self.summarized,
            "mode": self.mode,
        }

    def timeline_label(self) -> str:
        if self.mode == "passthrough":
            return f"上下文就绪 · {self.raw_messages} 条"
        if self.summarized:
            return (
                f"上下文压缩 · 摘要+保留 {self.kept_messages}/{self.raw_messages} · "
                f"~{self.approx_tokens_after} tok"
            )
        return (
            f"上下文裁剪 · 保留 {self.kept_messages}/{self.raw_messages} · "
            f"~{self.approx_tokens_after} tok"
        )


@dataclass
class ContextPack:
    """给 LLM 的消息包 + 可写回 state 的摘要/元信息。"""

    messages: List[BaseMessage]
    summary: str = ""
    meta: ContextMeta = field(default_factory=ContextMeta)


def estimate_tokens(text: str) -> int:
    """粗估 token：中英混排按 ~2 字符/token 更贴近；纯英文偏保守。"""
    if not text:
        return 0
    # 中文密度更高：汉字按 1 tok，其余按 4 字符 1 tok
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4) if other else cjk


def message_text(msg: Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict) and p.get("type") == "text":
                parts.append(str(p.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def message_tokens(msg: Any) -> int:
    n = estimate_tokens(message_text(msg))
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        n += estimate_tokens(str(tool_calls))
    name = getattr(msg, "name", None) or getattr(msg, "type", "") or ""
    return n + estimate_tokens(str(name)) + 4  # role 开销


def truncate_tool_content(content: str, max_chars: int) -> Tuple[str, bool]:
    """截断超长工具输出，尾部留标记。"""
    if max_chars <= 0 or len(content) <= max_chars:
        return content, False
    keep = max(32, max_chars - 80)
    head = content[:keep]
    omitted = len(content) - keep
    return f"{head}\n\n…[truncated {omitted} chars for context budget]…", True


def truncate_messages_tool_outputs(
    messages: Sequence[Any],
    max_chars: int,
) -> Tuple[List[Any], int]:
    """复制一份消息列表，仅截断 ToolMessage 内容（不改原对象）。"""
    out: List[Any] = []
    truncated = 0
    for m in messages:
        if isinstance(m, ToolMessage) or getattr(m, "type", "") == "tool":
            text = message_text(m)
            new_text, did = truncate_tool_content(text, max_chars)
            if did:
                truncated += 1
                out.append(
                    ToolMessage(
                        content=new_text,
                        tool_call_id=getattr(m, "tool_call_id", "") or "tool",
                        name=getattr(m, "name", None),
                    )
                )
            else:
                out.append(m)
        else:
            out.append(m)
    return out, truncated


def _msg_type(msg: Any) -> str:
    if isinstance(msg, HumanMessage):
        return "human"
    if isinstance(msg, AIMessage):
        return "ai"
    if isinstance(msg, ToolMessage):
        return "tool"
    if isinstance(msg, SystemMessage):
        return "system"
    return str(getattr(msg, "type", "") or "")


def _has_tool_calls(msg: Any) -> bool:
    return bool(getattr(msg, "tool_calls", None))


def _select_recent_window(
    messages: Sequence[Any],
    *,
    max_tokens: int,
    keep_min: int,
) -> Tuple[List[Any], List[Any]]:
    """从尾部向前取窗口，保证 tool 配对完整。

    返回 (kept, dropped_older)。
    """
    if not messages:
        return [], []

    n = len(messages)
    # 至少保留 keep_min 条（从尾部），并尽量把配对补全
    start = max(0, n - max(keep_min, 1))

    # 若窗口起点落在 ToolMessage 上，向前扩到带 tool_calls 的 AI
    while start > 0:
        m = messages[start]
        if _msg_type(m) == "tool":
            start -= 1
            continue
        break
    # 若 start 处是 tool 的后续，再确认前一条 AI 有 tool_calls
    if start > 0 and _msg_type(messages[start]) == "tool":
        for i in range(start - 1, -1, -1):
            if _has_tool_calls(messages[i]):
                start = i
                break
            if _msg_type(messages[i]) == "human":
                start = i
                break

    kept = list(messages[start:])
    # 再按 token 预算从头部继续丢（仍保配对）
    while len(kept) > keep_min:
        total = sum(message_tokens(m) for m in kept)
        if total <= max_tokens:
            break
        # 丢掉最旧一条；若丢掉 AI(tool_calls)，连同其后连续 ToolMessage
        drop_n = 1
        if _has_tool_calls(kept[0]):
            drop_n = 1
            while drop_n < len(kept) and _msg_type(kept[drop_n]) == "tool":
                drop_n += 1
        elif _msg_type(kept[0]) == "tool":
            # 孤立 tool 不应出现；丢掉
            drop_n = 1
        if drop_n >= len(kept):
            break
        kept = kept[drop_n:]

    dropped = list(messages[: n - len(kept)])
    return kept, dropped


def _heuristic_summary(dropped: Sequence[Any], prev_summary: str = "") -> str:
    """无 LLM 时的廉价摘要：保留用户句 + 工具名 + 短片段。"""
    lines: List[str] = []
    if prev_summary.strip():
        lines.append(prev_summary.strip())
    for m in dropped:
        t = _msg_type(m)
        text = message_text(m).strip().replace("\n", " ")
        if not text:
            continue
        if t == "human":
            lines.append(f"用户: {text[:200]}")
        elif t == "ai" and not _has_tool_calls(m):
            if text.startswith("["):
                lines.append(f"系统标记: {text[:120]}")
            else:
                lines.append(f"助手: {text[:160]}")
        elif t == "ai" and _has_tool_calls(m):
            names = [tc.get("name") for tc in (getattr(m, "tool_calls", None) or []) if tc.get("name")]
            lines.append(f"调用工具: {', '.join(names) or '?'}")
        elif t == "tool":
            name = getattr(m, "name", "") or "tool"
            lines.append(f"工具结果[{name}]: {text[:120]}")
    # 去重保序，限制长度
    seen = set()
    uniq: List[str] = []
    for ln in lines:
        if ln in seen:
            continue
        seen.add(ln)
        uniq.append(ln)
    blob = "\n".join(uniq[-24:])
    if len(blob) > 2400:
        blob = blob[:2400] + "…"
    return blob


async def llm_summarize(
    dropped: Sequence[Any],
    *,
    prev_summary: str = "",
    llm: Any = None,
) -> str:
    """用 LLM 压缩被丢掉的历史；失败则启发式。"""
    seed = _heuristic_summary(dropped, prev_summary=prev_summary)
    if llm is None or not dropped:
        return seed
    try:
        prompt = (
            "将以下 Agent 对话历史压缩为简洁中文要点（保留：用户目标、关键文件/PR、"
            "已用工具与关键结论、未完成事项）。不超过 400 字，不要 markdown。\n\n"
            f"{seed}"
        )
        from app.services.llm_usage.context import llm_call_context
        from app.services.llm_usage.recorder import ainvoke_tracked, primary_provider_info

        provider, model = primary_provider_info()
        req = getattr(llm, "model_name", None) or getattr(llm, "model", None) or model
        with llm_call_context(
            module_name="chat.context", node_name="compact_context", label="compact_context"
        ):
            resp = await ainvoke_tracked(
                llm,
                [HumanMessage(content=prompt)],
                provider=provider,
                requested_model=str(req),
                label="compact_context",
                call_type="chat",
            )
        text = message_text(resp).strip()
        return text or seed
    except Exception as exc:  # noqa: BLE001
        logger.warning("context summarize fallback: %s", exc)
        return seed


def build_context_pack(
    messages: Sequence[Any],
    *,
    existing_summary: str = "",
    max_tokens: int = 6000,
    keep_recent: int = 12,
    tool_max_chars: int = 4000,
    force_summary: Optional[str] = None,
) -> ContextPack:
    """同步组装（摘要已算好时用）。不调用 LLM。"""
    raw = list(messages or [])
    meta = ContextMeta(raw_messages=len(raw))
    meta.approx_tokens_before = sum(message_tokens(m) for m in raw)

    capped, tool_cut = truncate_messages_tool_outputs(raw, tool_max_chars)
    meta.tool_truncated = tool_cut

    budget = max(500, int(max_tokens))
    total = sum(message_tokens(m) for m in capped)
    if total <= budget and not force_summary and not (existing_summary or "").strip():
        meta.kept_messages = len(capped)
        meta.approx_tokens_after = total
        meta.mode = "passthrough" if tool_cut == 0 else "trim"
        return ContextPack(messages=capped, summary=existing_summary or "", meta=meta)

    kept, dropped = _select_recent_window(
        capped, max_tokens=budget, keep_min=max(4, int(keep_recent))
    )
    summary = (force_summary if force_summary is not None else existing_summary) or ""
    if dropped and not summary.strip():
        summary = _heuristic_summary(dropped, prev_summary=existing_summary)
    elif dropped and summary.strip() and force_summary is None:
        # 已有摘要时合并启发式增量
        summary = _heuristic_summary(dropped, prev_summary=summary)

    packed: List[BaseMessage] = []
    if summary.strip():
        packed.append(
            SystemMessage(
                content=f"{SUMMARY_PREFIX}\n以下是更早对话的压缩摘要，请结合后续原文继续任务：\n{summary.strip()}"
            )
        )
        meta.summarized = True
        meta.mode = "summarize+trim"
    else:
        meta.mode = "trim"

    packed.extend(kept)
    meta.kept_messages = len(kept)
    meta.dropped_messages = len(dropped)
    meta.approx_tokens_after = sum(message_tokens(m) for m in packed)
    return ContextPack(messages=packed, summary=summary.strip(), meta=meta)


async def compact_context(
    messages: Sequence[Any],
    *,
    existing_summary: str = "",
    max_tokens: int = 6000,
    keep_recent: int = 12,
    tool_max_chars: int = 4000,
    enable_llm_summary: bool = True,
    llm: Any = None,
) -> ContextPack:
    """完整压缩流水线：超预算时可选 LLM 摘要。"""
    raw = list(messages or [])
    capped, _ = truncate_messages_tool_outputs(raw, tool_max_chars)
    budget = max(500, int(max_tokens))
    total = sum(message_tokens(m) for m in capped)

    if total <= budget and not (existing_summary or "").strip():
        return build_context_pack(
            raw,
            existing_summary=existing_summary,
            max_tokens=max_tokens,
            keep_recent=keep_recent,
            tool_max_chars=tool_max_chars,
        )

    kept, dropped = _select_recent_window(
        capped, max_tokens=budget, keep_min=max(4, int(keep_recent))
    )
    summary = existing_summary or ""
    if dropped:
        if enable_llm_summary and llm is not None:
            summary = await llm_summarize(dropped, prev_summary=summary, llm=llm)
        else:
            summary = _heuristic_summary(dropped, prev_summary=summary)

    return build_context_pack(
        raw,
        existing_summary=summary,
        max_tokens=max_tokens,
        keep_recent=keep_recent,
        tool_max_chars=tool_max_chars,
        force_summary=summary,
    )
