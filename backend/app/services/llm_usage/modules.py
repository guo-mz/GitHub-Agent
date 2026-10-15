"""label / 节点 → 稳定 module_name + node_name。"""

from __future__ import annotations

from typing import Dict, Tuple

# label（ainvoke_llm / 包装器）→ (module_name, node_name)
_LABEL_MAP: Dict[str, Tuple[str, str]] = {
    "classify_intent": ("chat.classify", "classify_intent"),
    "planner": ("chat.planner", "planner"),
    "agent": ("chat.agent", "agent"),
    "agent_degrade": ("chat.agent", "agent_degrade"),
    "finalize": ("chat.finalize", "finalize"),
    "compact_context": ("chat.context", "compact_context"),
    "context_summary": ("chat.context", "compact_context"),
    "analysis_llm": ("analysis", "repo_analysis"),
    "planning_llm": ("planning", "planning_agent"),
    "coding_llm": ("coding", "coding_agent"),
    "coding": ("coding", "coding_agent"),
    "ai_edit": ("editor", "ai_edit"),
    "ai_edit_stream": ("editor", "ai_edit_stream"),
    "review_file": ("review", "review_file"),
    "review_pr": ("review", "review_pr"),
    "suggest_fix": ("fix", "suggest_fix"),
    "live-date-ai-edit": ("eval", "live_date_probe"),
    "embed": ("rag", "embed"),
    "llm": ("unknown", "llm"),
}

# LangGraph 节点名兜底
_NODE_MAP: Dict[str, Tuple[str, str]] = {
    "classify_intent": ("chat.classify", "classify_intent"),
    "planner": ("chat.planner", "planner"),
    "agent": ("chat.agent", "agent"),
    "finalize": ("chat.finalize", "finalize"),
    "compact_context": ("chat.context", "compact_context"),
    "repo_analysis": ("analysis", "repo_analysis"),
    "planning_agent": ("planning", "planning_agent"),
    "coding_agent": ("coding", "coding_agent"),
}


def resolve_module_node(
    *,
    label: str = "",
    module_name: str = "",
    node_name: str = "",
) -> Tuple[str, str]:
    if module_name and node_name:
        return module_name, node_name
    lab = (label or "").strip()
    if lab in _LABEL_MAP:
        m, n = _LABEL_MAP[lab]
        return module_name or m, node_name or n
    # 模糊：coding 在 label 中
    if "coding" in lab.lower():
        return module_name or "coding", node_name or lab or "coding_agent"
    if node_name and node_name in _NODE_MAP:
        m, n = _NODE_MAP[node_name]
        return module_name or m, node_name or n
    if module_name:
        return module_name, node_name or lab or "unknown"
    if lab:
        return "other", lab
    return "unknown", node_name or "unknown"
