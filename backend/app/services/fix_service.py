"""根据审查结论生成修复后的完整文件内容（不直接写仓库）。"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.llm_utils import make_chat_llm
from app.services.llm_usage.context import llm_call_context
from app.services.llm_usage.recorder import ainvoke_tracked, primary_provider_info

FIX_SYSTEM = """你是资深工程师。根据「原文件」和「审查/扫描问题」，输出修复后的完整文件内容。

硬性规则：
1. 只输出修复后的完整源码，不要 Markdown 围栏，不要解释
2. 保持原文件主要结构与风格，只改必要处
3. 不要删除无关功能；不要引入新依赖除非必须
4. 若问题无法在单文件内安全修复，尽量做防御性最小改动并在文件顶部用一行注释标明 # REVIEW-FIX: ...
"""


async def suggest_fixed_content(*, path: str, content: str, issues: str) -> str:
    prompt = (
        f"文件路径：{path}\n\n"
        f"## 需要修复的问题\n{issues[:12000]}\n\n"
        f"## 原文件\n```\n{content[:32000]}\n```\n"
    )
    provider, model = primary_provider_info()
    llm = make_chat_llm(temperature=0.1, streaming=False)
    with llm_call_context(module_name="fix", node_name="suggest_fix", label="suggest_fix"):
        resp = await ainvoke_tracked(
            llm,
            [SystemMessage(content=FIX_SYSTEM), HumanMessage(content=prompt)],
            provider=provider,
            requested_model=model,
            label="suggest_fix",
            call_type="chat",
        )
    text = (resp.content or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text
