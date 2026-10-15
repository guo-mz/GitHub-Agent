"""请求/响应 Pydantic v2 模型 —— API 契约层，前后端共享语义。"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    READ = "read"
    WRITE = "write"
    QA = "qa"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    repo: str = Field(..., description="owner/repo")
    thread_id: Optional[str] = Field(default=None, description="会话线程，缺省自动生成")
    resume: bool = Field(default=False, description="是否从检查点恢复")


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalRequest(BaseModel):
    decision: ApprovalDecision
    comment: Optional[str] = None
    # plan_confirm | publish | diff_approve；用于防止错阶段 resume
    approval_kind: Optional[str] = Field(default=None, description="plan_confirm | publish | ...")
    task_id: Optional[str] = Field(default=None, description="应与 thread_id / task_id 一致")


class ApprovalItem(BaseModel):
    thread_id: str
    operation: str
    risk_level: RiskLevel
    repo: str
    summary: str
    diff: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "pending"
    approval_kind: str = "publish"
    task_id: str = ""
    phase: str = ""


class SSEEventType(str, Enum):
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    APPROVAL_NEEDED = "approval_needed"
    ERROR = "error"
    DONE = "done"
    NODE = "node"
    TASK_STATUS = "task_status"
    RETRY = "retry"
    PLAN_CONFIRM_NEEDED = "plan_confirm_needed"
    DIFF_APPROVE_NEEDED = "diff_approve_needed"


class TaskRunStatus(str, Enum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    EDITING = "editing"
    CHECKING = "checking"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CODING = "awaiting_coding"  # 计划已确认，等待执行（≠ 修改成功）
    DIFF_APPROVED = "diff_approved"  # Diff 已确认，尚未发布
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SSEEvent(BaseModel):
    event: SSEEventType
    data: Dict[str, Any] = Field(default_factory=dict)
    thread_id: str = ""
    node: str = ""
    trace_id: str = ""


class RepoTreeNode(BaseModel):
    path: str
    type: str  # file | dir
    sha: Optional[str] = None
    size: Optional[int] = None


class RepoTreeResponse(BaseModel):
    repo: str
    branch: str
    nodes: List[RepoTreeNode]


class FileContentResponse(BaseModel):
    repo: str
    path: str
    ref: str
    sha: str
    content: str
    html_url: Optional[str] = None
    size: Optional[int] = None


class FileSaveRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str
    message: str = Field(default="chore: update via GitHub Agent editor")
    base_branch: str = Field(default="main", description="读取/分叉来源分支")
    target_branch: str = Field(
        default="",
        description="写入分支；空则自动创建 agent/edit-*；禁止直接写 main/master",
    )
    sha: Optional[str] = Field(default=None, description="乐观锁；空则按路径重新拉取")
    open_pr: bool = Field(default=False, description="保存后是否创建 PR（需 Contents/PR 权限）")
    pr_title: str = Field(default="")
    pr_body: str = Field(default="")


class FileSaveResponse(BaseModel):
    ok: bool
    path: str
    branch: str
    commit_sha: Optional[str] = None
    content_sha: Optional[str] = None
    html_url: Optional[str] = None
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    detail: str = ""


class AiEditRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str
    instruction: str = Field(..., min_length=1, max_length=4000)


class AiEditResponse(BaseModel):
    ok: bool
    path: str
    content: str
    detail: str = ""


class HealthResponse(BaseModel):
    status: str
    redis: bool
    postgres: bool
    rag: bool = False
    rag_mode: str = "none"  # pg | local | none
    checkpointer: str = "none"  # redis | memory | none
    rerank: str = "keyword"  # transformer | keyword | off
    version: str = "1.0.0"
    mode: str = "monolith"  # monolith | micro | agent | repo | gateway
    services: Optional[Dict[str, Any]] = None  # gateway 聚合子服务状态
    discovery: Optional[Dict[str, Any]] = None  # 服务发现快照
    breakers: Optional[Dict[str, Any]] = None  # 网关熔断状态
    persistence: Optional[Dict[str, Any]] = None  # checkpoint / 幂等 / HITL 能力
    auth_mode: str = "fail_closed"  # configured | insecure_open | fail_closed


class WebhookAck(BaseModel):
    ok: bool
    detail: str = ""
