"""LLM 多层兜底：阿里云百炼 → DeepSeek → Ollama。

任一层超时 / 连接失败 / 鉴权失败 / 5xx 则切下一层；
全部失败再抛给上层做启发式降级。

每次真实尝试经 llm_usage 独立记录（Token / 耗时 / 失败）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmProvider:
    name: str
    api_key: str
    base_url: str
    model: str


def _providers() -> List[LlmProvider]:
    s = get_settings()
    out: List[LlmProvider] = []

    # 1) 默认：阿里云百炼（OpenAI 兼容）—— 用 LLM_* 主配置
    if (s.llm_api_key or "").strip() and (s.llm_base_url or "").strip():
        out.append(
            LlmProvider(
                name="aliyun",
                api_key=s.llm_api_key.strip(),
                base_url=s.llm_base_url.strip().rstrip("/"),
                model=(s.llm_model or "qwen-plus").strip(),
            )
        )

    # 2) DeepSeek（独立配置）
    if (s.deepseek_api_key or "").strip():
        out.append(
            LlmProvider(
                name="deepseek",
                api_key=s.deepseek_api_key.strip(),
                base_url=(s.deepseek_base_url or "https://api.deepseek.com").strip().rstrip("/"),
                model=(s.deepseek_model or "deepseek-chat").strip(),
            )
        )

    # 3) Ollama 本地兜底
    if (s.ollama_base_url or "").strip():
        out.append(
            LlmProvider(
                name="ollama",
                api_key=(s.ollama_api_key or "ollama").strip(),
                base_url=s.ollama_base_url.strip().rstrip("/"),
                model=(s.ollama_model or "qwen2.5:7b").strip(),
            )
        )

    return out


def any_llm_configured() -> bool:
    return bool(_providers())


def make_chat_llm(
    *,
    temperature: float = 0.1,
    streaming: bool = False,
    provider: Optional[LlmProvider] = None,
    stream_usage: bool = False,
) -> ChatOpenAI:
    s = get_settings()
    timeout = float(getattr(s, "llm_request_timeout", 25) or 25)
    providers = _providers()
    p = provider or (providers[0] if providers else None)
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "streaming": streaming,
        "timeout": timeout,
        "max_retries": 0,
    }
    # 流式时请求终态 usage（服务商/SDK 支持时才有）
    if streaming or stream_usage:
        kwargs["stream_usage"] = True
    if p is None:
        return ChatOpenAI(
            api_key="sk-placeholder",
            base_url=s.llm_base_url,
            model=s.llm_model,
            **kwargs,
        )
    return ChatOpenAI(
        api_key=p.api_key or "sk-placeholder",
        base_url=p.base_url,
        model=p.model,
        **kwargs,
    )


def _is_failover_error(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    markers = (
        "timeout",
        "timed out",
        "connection",
        "unavailable",
        "overloaded",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "401",
        "403",
        "invalid",
        "unauthorized",
        "not found",
        "apikey",
        "api key",
        "incorrect",
    )
    return any(m in name for m in markers) or any(m in msg for m in markers)


async def ainvoke_llm(
    messages: Sequence[BaseMessage],
    *,
    temperature: float = 0.1,
    label: str = "llm",
    timeout: float | None = None,
) -> Any:
    """按 阿里云 → DeepSeek → Ollama 依次尝试；每次尝试独立记入 llm_usage。"""
    from app.services.llm_usage.context import llm_call_context
    from app.services.llm_usage.recorder import ainvoke_tracked

    s = get_settings()
    base_timeout = float(getattr(s, "llm_request_timeout", 25) or 25)
    # 编码任务上下文更大，默认给更长超时
    if timeout is None:
        timeout = base_timeout * 2.5 if "coding" in (label or "") else base_timeout
    timeout = max(15.0, float(timeout))
    providers = _providers()
    if not providers:
        raise RuntimeError("未配置任何 LLM（阿里云 / DeepSeek / Ollama）")

    call_group_id = uuid.uuid4().hex
    errors: List[str] = []
    with llm_call_context(label=label):
        for attempt_index, p in enumerate(providers):
            llm = make_chat_llm(temperature=temperature, streaming=False, provider=p)
            try:
                logger.info(
                    "%s trying provider=%s model=%s timeout=%.0fs",
                    label,
                    p.name,
                    p.model,
                    timeout,
                )
                resp = await ainvoke_tracked(
                    llm,
                    messages,
                    provider=p.name,
                    requested_model=p.model,
                    label=label,
                    call_type="chat",
                    call_group_id=call_group_id,
                    attempt_index=attempt_index,
                    retry_count=attempt_index,
                    timeout=timeout,
                )
                logger.info("%s provider=%s succeeded", label, p.name)
                return resp
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{p.name}:{type(exc).__name__}:{str(exc)[:160]}")
                logger.warning("%s provider=%s failed, failover: %s", label, p.name, exc)
                continue

    raise RuntimeError(f"{label} 全部 LLM 失败: {' | '.join(errors)}")
