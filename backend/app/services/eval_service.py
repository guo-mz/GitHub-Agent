"""评测执行：意图准确率 + 金标接地率 + 幻觉检出率 + 可选 live。

面试口径：
- intent_accuracy：规则意图判对率
- golden_groundedness：金标回答通过率（应接近 100%）
- hallucination_detection_rate：故意幻觉对照被正确检出的比例
- live_hallucination_rate：真调 LLM 的日期探针失败率（无 Key 则 null）
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services.eval_cases import EvalCase, grounding_cases, intent_cases
from app.services.intent import classify_by_rules

logger = logging.getLogger(__name__)
_TZ_SH = timezone(timedelta(hours=8))


def today_str() -> str:
    return datetime.now(_TZ_SH).strftime("%Y-%m-%d")


def _norm(s: str) -> str:
    return (s or "").strip()


def score_grounding(case: EvalCase, answer: Optional[str] = None) -> Dict[str, Any]:
    text = _norm(answer if answer is not None else str(case.get("answer") or ""))
    today = today_str()
    if text == "PLACEHOLDER_TODAY":
        text = f"hello from agent\n\n{today}"

    reasons: List[str] = []
    grounded = True

    must = list(case.get("must_contain") or [])
    if case.get("must_contain_today"):
        must = [*must, today]
    for needle in must:
        if needle and needle not in text:
            grounded = False
            reasons.append(f"缺少必含「{needle}」")

    any_need = list(case.get("must_contain_any") or [])
    if any_need and not any(n in text for n in any_need if n):
        grounded = False
        reasons.append(f"未命中任一必含 {any_need}")

    for bad in case.get("must_not_contain") or []:
        if bad and bad in text:
            grounded = False
            reasons.append(f"命中禁出（疑似幻觉）「{bad}」")

    if case.get("must_contain_today") and today not in text:
        others = [d for d in re.findall(r"20\d{2}-\d{2}-\d{2}", text) if d != today]
        if others:
            grounded = False
            if f"缺少必含「{today}」" not in reasons:
                reasons.append(f"使用了非当日日期 {others}")

    expect_pass = bool(case.get("expect_pass", True))
    # 计分器判定是否与期望一致
    aligned = grounded == expect_pass
    return {
        "id": case.get("id"),
        "suite": "grounding",
        "title": case.get("title"),
        "category": case.get("category"),
        "expect_pass": expect_pass,
        "grounded": grounded,
        "pass": aligned,  # 校准通过：金标应 grounded，对照应 !grounded
        "hallucinated": not grounded,
        "reasons": reasons,
        "answer_preview": text[:240],
        "today": today,
    }


def score_intent(case: EvalCase) -> Dict[str, Any]:
    user = str(case.get("user") or "")
    expect = str(case.get("expect_task_type") or "")
    got = classify_by_rules(user)
    actual = got.task_type if got else "qa"
    conf = got.confidence if got else 0.0
    source = got.source if got else "fallback"
    ok = actual == expect
    return {
        "id": case.get("id"),
        "suite": "intent",
        "title": case.get("title"),
        "category": case.get("category"),
        "pass": ok,
        "expect": expect,
        "actual": actual,
        "confidence": conf,
        "source": source,
        "user": user,
    }


async def run_live_date_probe() -> Dict[str, Any]:
    from app.config import get_settings
    from app.services.ai_edit import ai_edit_buffer

    settings = get_settings()
    today = today_str()
    case_id = "live-date-ai-edit"
    if not (settings.llm_api_key or "").strip():
        return {
            "id": case_id,
            "suite": "live",
            "title": "编辑器写入当前日期（live）",
            "pass": None,
            "skipped": True,
            "reason": "无 LLM_API_KEY，跳过 live",
            "today": today,
            "hallucinated": None,
        }
    try:
        out = await ai_edit_buffer(
            path="docs/hello.md",
            content="hello from agent\n",
            instruction="在里面加上当前日期",
        )
        ok = today in (out or "")
        reasons = [] if ok else [f"输出未含今天 {today}", f"preview={(out or '')[:120]}"]
        return {
            "id": case_id,
            "suite": "live",
            "title": "编辑器写入当前日期（live）",
            "pass": ok,
            "hallucinated": not ok,
            "skipped": False,
            "reasons": reasons,
            "answer_preview": (out or "")[:240],
            "today": today,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("live date probe failed: %s", exc)
        return {
            "id": case_id,
            "suite": "live",
            "title": "编辑器写入当前日期（live）",
            "pass": False,
            "hallucinated": True,
            "skipped": False,
            "reasons": [str(exc)],
            "today": today,
        }


def _pct(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return round(num / den, 4)


async def run_eval(
    *,
    suites: Optional[List[str]] = None,
    include_live: bool = False,
) -> Dict[str, Any]:
    want = set(suites or ["intent", "grounding"])
    results: List[Dict[str, Any]] = []

    if "intent" in want:
        for c in intent_cases():
            results.append(score_intent(c))

    if "grounding" in want:
        for c in grounding_cases():
            results.append(score_grounding(c))

    if include_live or "live" in want:
        results.append(await run_live_date_probe())

    intent_rows = [r for r in results if r.get("suite") == "intent"]
    ground_rows = [r for r in results if r.get("suite") == "grounding"]
    live_rows = [r for r in results if r.get("suite") == "live" and not r.get("skipped")]

    intent_acc = _pct(sum(1 for r in intent_rows if r.get("pass")), len(intent_rows))

    golden = [r for r in ground_rows if r.get("expect_pass")]
    adversarial = [r for r in ground_rows if not r.get("expect_pass")]
    golden_groundedness = _pct(sum(1 for r in golden if r.get("grounded")), len(golden))
    hall_detect = _pct(
        sum(1 for r in adversarial if r.get("hallucinated")),
        len(adversarial),
    )
    # 金标上「未接地」占比 ≈ 金标幻觉率（样例级）
    golden_hall_rate = None
    if golden_groundedness is not None:
        golden_hall_rate = round(1.0 - golden_groundedness, 4)

    live_hall = None
    if live_rows:
        live_hall = _pct(sum(1 for r in live_rows if r.get("hallucinated")), len(live_rows))

    return {
        "ok": True,
        "generated_at": datetime.now(_TZ_SH).isoformat(),
        "today": today_str(),
        "metrics": {
            "intent_accuracy": intent_acc,
            "golden_groundedness": golden_groundedness,
            "golden_hallucination_rate": golden_hall_rate,
            "hallucination_detection_rate": hall_detect,
            "live_hallucination_rate": live_hall,
            "intent_n": len(intent_rows),
            "golden_n": len(golden),
            "adversarial_n": len(adversarial),
            "live_n": len(live_rows),
        },
        "results": results,
        "note": (
            "golden_hallucination_rate：金标样例未接地占比；"
            "hallucination_detection_rate：故意幻觉对照检出率；"
            "live_hallucination_rate：真调 LLM（需 Key）。"
        ),
    }
