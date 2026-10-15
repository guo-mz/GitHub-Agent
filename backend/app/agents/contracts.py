"""多 Agent 结构化契约 —— 禁止仅用自然语言传递阶段结果。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApprovalKind(str, Enum):
    """审批类型必须区分，防止 resume 错阶段。"""

    PLAN_CONFIRM = "plan_confirm"
    DIFF_APPROVE = "diff_approve"  # Phase 2
    PUBLISH = "publish"  # 现有 create_pr / merge


class AgentName(str, Enum):
    ORCHESTRATOR = "orchestrator"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    CODING = "coding"
    REVIEW = "review"
    TESTING = "testing"
    PUBLISH = "publish"


class AgentEnvelope(BaseModel):
    task_id: str = ""
    agent_name: str
    status: str = Field(description="queued|running|succeeded|failed|cancelled|skipped|not_run")
    input_context: Dict[str, Any] = Field(default_factory=dict)
    structured_output: Dict[str, Any] = Field(default_factory=dict)
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    started_at: str = Field(default_factory=utc_now)
    finished_at: Optional[str] = None


class RelevantFile(BaseModel):
    path: str
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class AnalysisReport(BaseModel):
    repo: str
    ref: str = "main"
    tech_stack: List[str] = Field(default_factory=list)
    key_modules: List[str] = Field(default_factory=list)
    entrypoints: List[str] = Field(default_factory=list)
    dependencies_summary: List[str] = Field(default_factory=list)
    relevant_files: List[RelevantFile] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    raw_tree_sample: List[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    step: int
    action: str
    files: List[str] = Field(default_factory=list)
    risk: str = ""


class PlanDocument(BaseModel):
    title: str = ""
    goals: List[str] = Field(default_factory=list)
    files_to_change: List[str] = Field(default_factory=list)
    steps: List[PlanStep] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    verification: List[str] = Field(default_factory=list)
    out_of_scope: List[str] = Field(default_factory=list)
    exceeds_request: bool = False
    exceeds_reason: str = ""


class CheckResult(BaseModel):
    """Testing Agent 检查项；未执行必须 not_run。"""

    name: str
    status: str = Field(description="passed|failed|not_run|skipped")
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    log: str = ""
    command: str = ""
