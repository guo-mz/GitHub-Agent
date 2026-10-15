"""Jev（TypeSafe System One）决策层 —— 快、便宜、typed，不生成长文本。

有 TYPESAFE_API_KEY 时走官方 API；无密钥时用本地启发式兜底（面试/离线可演示）。
面试点：LLM 负责生成与推理；Jev 负责有界选择 / 风险打分 / 是否升级 HITL。
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class JevAnswer:
    field: str
    type: str  # choice | boolean | score
    value: Any
    confidence: Optional[float] = None
    probabilities: Optional[Dict[str, float]] = None


@dataclass
class JevBundle:
    mode: str  # api | local
    model: str
    answers: Dict[str, JevAnswer] = field(default_factory=dict)
    requires_review: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "model": self.model,
            "requires_review": self.requires_review,
            "answers": {k: asdict(v) for k, v in self.answers.items()},
        }

    def get(self, name: str, default: Any = None) -> Any:
        a = self.answers.get(name)
        return a.value if a else default

    def confidence(self, name: str, default: float = 0.0) -> float:
        a = self.answers.get(name)
        if not a or a.confidence is None:
            return default
        return float(a.confidence)


def _local_decide(user_text: str, task_hint: str = "") -> JevBundle:
    """无 API Key 时的启发式 System One：语义对齐 choice/score/noul。"""
    t = (user_text or "").lower()
    hint = (task_hint or "").lower()

    # task_type choice
    scores = {
        "review": 0.15,
        "write": 0.15,
        "read": 0.15,
        "qa": 0.15,
    }
    for k, keys in (
        ("review", ("审查", "review", "扫描", "bug", "安全")),
        ("write", ("修复", "提pr", "pr", "改代码", "写入", "merge", "分支")),
        ("read", ("列出", "查看", "issue", "commit", "搜索", "对比")),
        ("qa", ("是什么", "解释", "怎么用", "能干啥", "hello", "你好")),
    ):
        for kw in keys:
            if kw in t:
                scores[k] += 0.35
    if hint in scores:
        scores[hint] += 0.5
    total = sum(scores.values()) or 1.0
    probs = {k: v / total for k, v in scores.items()}
    task = max(probs, key=probs.get)
    task_conf = probs[task]

    # risk score 0..3
    risk = 0.3
    if any(k in t for k in ("merge", "合并", "删除", "rm ", "force")):
        risk = 2.8
    elif any(k in t for k in ("提pr", "pr", "写入", "修复", "改代码", "update")):
        risk = 2.0
    elif task == "write":
        risk = 1.8
    elif task == "review":
        risk = 1.0
    risk_conf = 0.75 if risk >= 1.5 else 0.65

    # escalate HITL (noul)
    escalate = 0.15
    if risk >= 2.5:
        escalate = 0.92
    elif risk >= 1.8 or task == "write":
        escalate = 0.78
    elif "审批" in t or "approve" in t:
        escalate = 0.7

    # need planner
    need_plan = 0.2
    if task in {"write", "review", "read"}:
        need_plan = 0.85
    if len(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]{4,}", t)) >= 6:
        need_plan = max(need_plan, 0.7)

    return JevBundle(
        mode="local",
        model="jev-local-heuristic",
        requires_review=escalate >= 0.75 and risk_conf < 0.9,
        answers={
            "task_type": JevAnswer(
                "task_type", "choice", task, task_conf, probs
            ),
            "risk": JevAnswer("risk", "score", round(risk, 2), risk_conf),
            "escalate_hitl": JevAnswer("escalate_hitl", "boolean", escalate, None),
            "need_planner": JevAnswer("need_planner", "boolean", need_plan, None),
        },
        raw={"note": "TYPESAFE_API_KEY 未配置，使用本地启发式兜底"},
    )


async def decide_agent_turn(
    *,
    user_text: str,
    repo: str = "",
    task_hint: str = "",
    intent: Optional[Dict[str, Any]] = None,
) -> JevBundle:
    """对一轮用户请求做 Jev 决策：任务类型 / 风险 / 是否升级人工。"""
    settings = get_settings()
    if not settings.jev_enabled:
        bundle = _local_decide(user_text, task_hint or (intent or {}).get("task_type", ""))
        bundle.mode = "disabled-local"
        return bundle

    api_key = (settings.typesafe_api_key or "").strip()
    state_obj = {
        "user_message": user_text,
        "repo": repo,
        "intent_hint": intent or {},
        "task_hint": task_hint,
    }

    if not api_key:
        return _local_decide(user_text, task_hint or (intent or {}).get("task_type", ""))

    try:
        from langgraph_jev import JevClient, boolean, choice, score

        questions = {
            "task_type": choice(
                {
                    "review": "Code review / security scan / find bugs",
                    "write": "Modify code, create branch, open or merge PR",
                    "read": "List issues/PRs/commits or read files",
                    "qa": "General question about capability or explanation",
                },
                instructions="Classify the user request for a GitHub R&D agent.",
            ),
            "risk": score(
                [
                    "Read-only / informational, no side effects",
                    "May change non-critical files on a feature branch",
                    "Will open PR or write multiple files",
                    "Merge to main / destructive / irreversible",
                ],
                instructions="How risky is executing this request end-to-end?",
            ),
            "escalate_hitl": boolean(
                instructions="Should a human approve before any write or PR action?"
            ),
            "need_planner": boolean(
                instructions="Does this need a multi-step plan before tool use?"
            ),
        }
        client = JevClient(api_key=api_key, model=settings.jev_model or "jev-latest")
        try:
            result = await client.adecide(state=state_obj, questions=questions)
        finally:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

        answers: Dict[str, JevAnswer] = {}
        for name, dec in (result.decisions or {}).items():
            answers[name] = JevAnswer(
                field=name,
                type=dec.type,
                value=dec.value,
                confidence=dec.confidence,
                probabilities=dec.probabilities,
            )
        thr = float(settings.jev_confidence_threshold)
        low = any(
            (a.confidence is not None and a.confidence < thr)
            for a in answers.values()
            if a.type in {"choice", "score"}
        )
        return JevBundle(
            mode="api",
            model=getattr(result, "model", settings.jev_model) or "jev",
            answers=answers,
            requires_review=bool(getattr(result, "requires_review", False) or low),
            raw={"usage": getattr(result, "usage", None)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Jev API failed, local fallback: %s", exc)
        bundle = _local_decide(user_text, task_hint or (intent or {}).get("task_type", ""))
        bundle.raw["api_error"] = str(exc)
        return bundle


def timeline_label(bundle: JevBundle) -> str:
    task = bundle.get("task_type", "?")
    risk = bundle.get("risk", 0)
    esc = bundle.get("escalate_hitl", 0)
    conf = bundle.confidence("task_type", 0)
    mode = "API" if bundle.mode == "api" else "本地"
    hitl = "·升HITL" if float(esc or 0) >= 0.7 else ""
    return f"Jev {mode} {task} 风险{risk}{hitl} {int(conf*100)}%"
