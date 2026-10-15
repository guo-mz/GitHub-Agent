"""评测 API —— 固定样例集跑意图准确率 / 接地率 / 幻觉相关指标。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.security_auth import require_operator
from app.services.eval_cases import cases_public
from app.services.eval_service import run_eval

router = APIRouter(prefix="/api/eval", tags=["eval"])


class EvalRunRequest(BaseModel):
    suites: Optional[List[str]] = Field(
        default=None,
        description="intent / grounding / live；默认 intent+grounding",
    )
    include_live: bool = Field(
        default=False,
        description="是否真调 LLM 做日期探针（需 LLM_API_KEY）",
    )


@router.get("/cases")
async def list_cases(_auth: None = Depends(require_operator)):
    return {"ok": True, "cases": cases_public()}


@router.post("/run")
async def eval_run(
    body: EvalRunRequest = EvalRunRequest(),
    _auth: None = Depends(require_operator),
):
    report = await run_eval(suites=body.suites, include_live=body.include_live)
    return report
