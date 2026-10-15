"""意图识别：规则快路径 + 槽位抽取 + 置信度。

面试点：先规则（可解释、快），模糊再 LLM；槽位（path/pr/branch）喂给 planner/agent。
越界快路径：明显无关软件开发的请求用纯规则短路，不调 LLM。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

TASK_TYPES = ("read", "write", "qa", "review")

OUT_OF_SCOPE_REPLY = (
    "我是专注于软件开发的 Coding Agent，可以帮你编写代码、排查 Bug 或分析项目。"
    "你可以告诉我需要解决的开发问题。"
)

_PR_NUM = re.compile(r"(?:#|pr\s*#?|pull request\s*#?|编号)\s*(\d+)", re.I)
_PATH = re.compile(
    r"(?:文件|path)\s*[`:]?\s*([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)"
    r"|`([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)`"
    r"|(?<![\w/])((?:src|docs|app|frontend|backend)/[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)"
    r"|(README(?:\.[A-Za-z0-9]+)?)",
    re.I,
)
_BRANCH = re.compile(
    r"(?:分支|branch)\s*[`:]?\s*([A-Za-z0-9._/-]+)"
    r"|`((?:agent|feat|fix|chore)/[A-Za-z0-9._/-]+)`",
    re.I,
)
_REPO = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")

# 独立纯算术：1+1 / 1 + 1 等于几 / 2*3=?
_PURE_ARITH = re.compile(
    r"^\s*\d+\s*[+\-*/×÷]\s*\d+\s*(?:等于|是|=)?\s*(?:几|多少|什么)?\s*[?？！!。.]?\s*$",
    re.I,
)
# 纯问候（整句）
_PURE_GREETING = re.compile(
    r"^\s*(?:你好|您好|哈喽|嗨|hi|hello|hey|早上好|晚上好|午安)\s*[!！.。~～]?\s*$",
    re.I,
)

WRITE_STRONG = (
    "审查并修复",
    "按审查",
    "根据审查",
    "修复并提",
    "修完提pr",
    "suggest_fix",
    "提pr",
    "提 pr",
    "pull request",
    "创建 pr",
    "建pr",
    "merge",
    "合并pr",
    "改代码",
    "修改代码",
    "提交代码",
    "创建分支",
    "建issue",
    "创建 issue",
    "commit",
)
REVIEW_KEYS = (
    "审查",
    "code review",
    "codereview",
    "找bug",
    "找 bug",
    "潜在bug",
    "代码质量",
    "安全扫描",
    "扫描安全",
    "密钥泄露",
    "优化建议",
)
WRITE_SOFT = ("修复", "fix", "改一下", "更新文件", "写入")
READ_KEYS = (
    "issue",
    "读",
    "查看",
    "list",
    "搜索",
    "search",
    "文件内容",
    "readme",
    "列出",
    "commits",
    "提交历史",
    "对比",
    "compare",
)
# 能力问询保留走正常 qa；纯「你好」由越界规则处理，故不把「你好/hello」单独当高置信 qa
QA_KEYS = ("能干啥", "你是谁", "能力", "怎么用", "是什么", "解释一下")

# 开发相关信号：有则绝不越界短路
_DEV_SIGNALS = (
    "代码",
    "code",
    "bug",
    "报错",
    "错误",
    "exception",
    "traceback",
    "函数",
    "方法",
    "类",
    "模块",
    "api",
    "接口",
    "数据库",
    "表结构",
    "sql",
    "架构",
    "性能",
    "优化",
    "重构",
    "测试",
    "unittest",
    "pytest",
    "github",
    "actions",
    "ci",
    "cd",
    "部署",
    "docker",
    "k8s",
    "kubernetes",
    "readme",
    "文档",
    "注释",
    "仓库",
    "repo",
    "分支",
    "branch",
    "commit",
    "pr",
    "pull request",
    "issue",
    "merge",
    "lint",
    "编译",
    "构建",
    "build",
    "依赖",
    "import",
    "package.json",
    "requirements",
    "python",
    "javascript",
    "typescript",
    "java",
    "golang",
    "rust",
    "前端",
    "后端",
    "算法",
    "设计模式",
    "类型",
    "类型错误",
    "null",
    "undefined",
    "这段",
    "这个项目",
    "这个函数",
    "这段代码",
)

_LIFE_KEYS = (
    "今天天气",
    "明天天气",
    "下雨吗",
    "怎么做饭",
    "食谱",
    "菜谱",
    "减肥",
    "恋爱",
    "相亲",
    "星座",
    "运势",
    "股票推荐",
    "买什么基金",
    "旅游攻略",
    "几点了",
    "讲个笑话",
    "陪我聊天",
)

_ENTERTAIN_KEYS = (
    "写一首诗",
    "写个故事",
    "编个段子",
    "写小说",
    "唱首歌",
    "讲笑话",
    "角色扮演",
    "玩成语接龙",
)

# 安全/越狱话术：不标为「无关闲聊」，交给原流水线
_SECURITY_OR_JAILBREAK = (
    "忽略以上",
    "忽略之前",
    "忽略系统",
    "无视规则",
    "忽略规则",
    "ignore previous",
    "ignore all",
    "ignore the rules",
    "jailbreak",
    "dan mode",
    "你现在是",
    "扮演成",
    "绕过限制",
    "绕过职责",
    "披露密钥",
    "泄露密钥",
    "api key",
    "api_key",
    "secret key",
    "密码是什么",
    "token 是",
    "删除所有文件",
    "rm -rf",
    "强制推送",
    "force push",
    "未经授权",
)


@dataclass
class IntentResult:
    task_type: str
    confidence: float
    source: str  # rules | llm | fallback | oos_rules
    reason: str
    slots: Dict[str, Any] = field(default_factory=dict)
    hits: List[str] = field(default_factory=list)
    out_of_scope: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def timeline_label(self) -> str:
        if self.out_of_scope:
            return "意图 职责外（规则）"
        names = {"qa": "问答", "read": "只读", "review": "审查", "write": "写入"}
        zh = names.get(self.task_type, self.task_type)
        pct = int(round(self.confidence * 100))
        extra = []
        if self.slots.get("path"):
            extra.append(str(self.slots["path"]))
        if self.slots.get("pr_number"):
            extra.append(f"#{self.slots['pr_number']}")
        if self.slots.get("branch"):
            extra.append(self.slots["branch"])
        tail = f" · {' '.join(extra)}" if extra else ""
        return f"意图 {zh} {pct}%{tail}"


def extract_slots(text: str, default_repo: str = "") -> Dict[str, Any]:
    slots: Dict[str, Any] = {}
    m = _PR_NUM.search(text or "")
    if m:
        slots["pr_number"] = int(m.group(1))
    path = None
    pm = _PATH.search(text or "")
    if pm:
        path = next((g for g in pm.groups() if g), None)
    if path:
        slots["path"] = path.replace("\\", "/")
    bm = _BRANCH.search(text or "")
    if bm:
        branch = next((g for g in bm.groups() if g), None)
        if branch:
            slots["branch"] = branch.strip("`")
    rm = _REPO.search(text or "")
    if rm and rm.group(1).count("/") == 1:
        cand = rm.group(1)
        if cand.lower() not in {"owner/repo", "pull/request"}:
            slots["repo"] = cand
    elif default_repo:
        slots["repo"] = default_repo
    return slots


def _contains_any(text: str, keys: Sequence[str]) -> List[str]:
    lower = (text or "").lower()
    return [k for k in keys if k.lower() in lower]


def _has_dev_signal(text: str) -> bool:
    raw = text or ""
    if extract_slots(raw):
        # 仅 default_repo 不算；有 path/pr/branch/repo 槽位才算
        slots = extract_slots(raw, default_repo="")
        if slots.get("path") or slots.get("pr_number") or slots.get("branch") or slots.get("repo"):
            return True
    if _contains_any(raw, _DEV_SIGNALS):
        return True
    # 代码块/后缀文件名
    if re.search(r"```|/\w+\.\w{1,8}\b|\.\w{1,8}\b", raw):
        if re.search(
            r"\.(py|js|ts|tsx|jsx|go|rs|java|kt|c|cpp|h|cs|rb|php|vue|md|yml|yaml|json|toml)\b",
            raw,
            re.I,
        ):
            return True
    return False


def detect_out_of_scope(text: str) -> Optional[IntentResult]:
    """明显无关软件开发时返回高置信越界结果；不确定则 None（继续正常流程）。"""
    raw = (text or "").strip()
    if not raw:
        return None

    if _contains_any(raw, _SECURITY_OR_JAILBREAK):
        return None
    if _has_dev_signal(raw):
        return None

    # 能力问询仍属职责内（不短路）
    if _contains_any(raw, QA_KEYS):
        return None

    if _PURE_GREETING.match(raw):
        return IntentResult(
            "qa",
            0.95,
            "oos_rules",
            "纯问候，职责外",
            hits=["greeting"],
            out_of_scope=True,
        )

    if _PURE_ARITH.match(raw):
        return IntentResult(
            "qa",
            0.95,
            "oos_rules",
            "独立纯算术，职责外",
            hits=["arith"],
            out_of_scope=True,
        )

    life = _contains_any(raw, _LIFE_KEYS)
    if life:
        return IntentResult(
            "qa",
            0.9,
            "oos_rules",
            "生活咨询，职责外",
            hits=life,
            out_of_scope=True,
        )

    ent = _contains_any(raw, _ENTERTAIN_KEYS)
    if ent and not _has_dev_signal(raw):
        return IntentResult(
            "qa",
            0.9,
            "oos_rules",
            "无关娱乐创作，职责外",
            hits=ent,
            out_of_scope=True,
        )

    return None


def has_active_dev_context(
    messages: Optional[Sequence[Any]] = None,
    state: Optional[Dict[str, Any]] = None,
) -> bool:
    """会话已有明确开发任务时，禁止对跟进闲聊/算术做越界短路。"""
    st = state or {}
    if st.get("plan") or st.get("plan_document") or st.get("workspace_diff"):
        return True
    if st.get("analysis_report") or st.get("workspace_id"):
        return True
    phase = str(st.get("phase") or "")
    if phase and phase not in {"", "queued"}:
        return True

    intent = st.get("intent") or {}
    if isinstance(intent, dict) and intent.get("task_type") in {"read", "write", "review"}:
        return True

    msgs = list(messages or [])
    human_count = 0
    for m in msgs:
        mtype = getattr(m, "type", "") or ""
        content = getattr(m, "content", "")
        text = content if isinstance(content, str) else str(content or "")
        # HumanMessage
        if mtype == "human" or m.__class__.__name__ == "HumanMessage":
            human_count += 1
            if human_count > 1 and _has_dev_signal(text):
                return True
            # 历史用户消息（非当前最后一条）含开发信号
            continue
        if text.startswith("[classify]"):
            if any(x in text for x in ("意图=write", "意图=review", "意图=read", "写入", "审查", "只读")):
                return True
            if "write" in text or "review" in text or "read" in text:
                # 格式：意图=write 或 IntentResult timeline
                if "职责外" not in text:
                    if re.search(r"意图\s*(写入|审查|只读)|意图=(write|review|read)", text):
                        return True
        if text.startswith("[jev]") and any(k in text for k in ("write", "review", "read", "写入", "审查")):
            return True

    # 除当前用户消息外，若更早的 human 含开发信号或开发意图
    humans: List[str] = []
    for m in msgs:
        mtype = getattr(m, "type", "") or ""
        if mtype == "human" or m.__class__.__name__ == "HumanMessage":
            content = getattr(m, "content", "")
            humans.append(content if isinstance(content, str) else str(content or ""))
    if len(humans) >= 2:
        for prev in humans[:-1]:
            if _has_dev_signal(prev):
                return True
            prev_rule = classify_by_rules(prev)
            if prev_rule and prev_rule.task_type in {"read", "write", "review"}:
                return True

    return False


def classify_by_rules(text: str) -> Optional[IntentResult]:
    raw = (text or "").strip()
    if not raw:
        return IntentResult("qa", 0.4, "rules", "空输入", {})
    lower = raw.lower()
    hits: List[str] = []

    def hit(keys: tuple[str, ...]) -> List[str]:
        return [k for k in keys if k.lower() in lower]

    write_s = hit(WRITE_STRONG)
    review = hit(REVIEW_KEYS)
    write_w = hit(WRITE_SOFT)
    read = hit(READ_KEYS)
    qa = hit(QA_KEYS)
    review_word = bool(re.search(r"(?<![a-z])review(?![a-z])", lower))
    if review_word:
        review.append("review")

    # 审查并修复 → write（计划里先审后改）
    if write_s:
        hits = write_s
        conf = 0.95 if any(k in lower for k in ["提pr", "提 pr", "pull request", "审查并修复"]) else 0.88
        return IntentResult("write", conf, "rules", "命中写入/提PR关键词", hits=hits)
    if review:
        return IntentResult("review", 0.9, "rules", "命中审查/扫描关键词", hits=review)
    if write_w:
        return IntentResult("write", 0.82, "rules", "命中修复/改文件关键词", hits=write_w)
    if read:
        return IntentResult("read", 0.8, "rules", "命中只读/检索关键词", hits=read)
    if qa:
        return IntentResult("qa", 0.85, "rules", "命中闲聊/能力问询", hits=qa)
    # 「你好，你能干啥」等：问候 + 能力问询
    if re.search(r"(你好|hello|hi)", lower) and any(
        k in lower for k in ("能干", "能力", "是谁", "怎么用")
    ):
        return IntentResult("qa", 0.85, "rules", "命中闲聊/能力问询", hits=["hello+capability"])
    return None


def merge_llm(rule: Optional[IntentResult], llm_type: str, llm_reason: str = "") -> IntentResult:
    t = (llm_type or "qa").lower()
    if t not in TASK_TYPES:
        t = "qa"
    if rule is None:
        return IntentResult(t, 0.7, "llm", llm_reason or "LLM 分类", hits=["llm"])
    if t == rule.task_type:
        return IntentResult(
            rule.task_type,
            min(0.99, rule.confidence + 0.05),
            "rules+llm",
            llm_reason or rule.reason,
            hits=rule.hits + ["llm_agree"],
            out_of_scope=rule.out_of_scope,
        )
    # 规则高置信不让 LLM 推翻
    if rule.confidence >= 0.85:
        return IntentResult(
            rule.task_type,
            rule.confidence,
            "rules",
            f"{rule.reason}（LLM 建议 {t}，未采纳）",
            hits=rule.hits,
            out_of_scope=rule.out_of_scope,
        )
    return IntentResult(
        t,
        0.72,
        "llm",
        llm_reason or f"LLM 覆盖规则 {rule.task_type}",
        hits=["llm_override"],
    )


def format_intent_hint(intent: IntentResult) -> str:
    if intent.out_of_scope:
        return f"意图=职责外；置信={intent.confidence:.2f}；来源={intent.source}"
    parts = [f"意图={intent.task_type}", f"置信={intent.confidence:.2f}", f"来源={intent.source}"]
    if intent.slots.get("path"):
        parts.append(f"文件={intent.slots['path']}")
    if intent.slots.get("pr_number"):
        parts.append(f"PR=#{intent.slots['pr_number']}")
    if intent.slots.get("branch"):
        parts.append(f"分支={intent.slots['branch']}")
    return "；".join(parts)
