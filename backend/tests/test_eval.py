"""评测：意图准确率 / 金标接地 / 幻觉检出。"""

import asyncio

from app.services.eval_service import run_eval, score_grounding, today_str
from app.services.eval_cases import grounding_cases


def test_today_format():
    t = today_str()
    assert len(t) == 10 and t[4] == "-" and t[7] == "-"


def test_golden_date_grounds():
    case = next(c for c in grounding_cases() if c["id"] == "ground-date-ok")
    r = score_grounding(case)
    assert r["grounded"] is True
    assert r["pass"] is True
    assert today_str() in r["answer_preview"]


def test_bad_date_detected_as_hallucination():
    case = next(c for c in grounding_cases() if c["id"] == "ground-date-bad")
    r = score_grounding(case)
    assert r["grounded"] is False
    assert r["hallucinated"] is True
    assert r["pass"] is True  # 期望失败且确实失败 → 校准通过


def test_run_eval_metrics():
    report = asyncio.run(run_eval(include_live=False))
    m = report["metrics"]
    assert m["intent_n"] == 6
    assert m["golden_n"] == 4
    assert m["adversarial_n"] == 4
    assert m["intent_accuracy"] == 1.0
    assert m["golden_groundedness"] == 1.0
    assert m["golden_hallucination_rate"] == 0.0
    assert m["hallucination_detection_rate"] == 1.0
