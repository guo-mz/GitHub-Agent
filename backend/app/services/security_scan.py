"""静态安全/质量启发式扫描 —— 与 LLM 审查互补，可解释、可演示。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass
class Finding:
    severity: str  # critical|high|medium|low|info
    rule: str
    message: str
    line: int
    snippet: str


RULES: List[tuple[str, str, re.Pattern[str], str]] = [
    ("critical", "hardcoded_secret", re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][^'\"]{8,}['\"]"), "疑似硬编码密钥/口令"),
    ("critical", "private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "检测到私钥材料"),
    ("high", "aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "疑似 AWS Access Key"),
    ("high", "sql_concat", re.compile(r"(?i)(execute|query|cursor\.execute)\s*\(\s*[f\"'].*(\+|%)"), "疑似 SQL 拼接，存在注入风险"),
    ("high", "eval_exec", re.compile(r"\b(eval|exec)\s*\("), "动态执行 eval/exec，存在代码注入风险"),
    ("high", "child_shell", re.compile(r"(?i)(os\.system|subprocess\.(call|run|Popen))\s*\([^)]*shell\s*=\s*True"), "subprocess shell=True 风险"),
    ("medium", "bare_except", re.compile(r"(?m)^\s*except\s*:\s*$"), "裸 except 会吞掉所有异常"),
    ("medium", "todo_fixme", re.compile(r"(?i)\b(TODO|FIXME|XXX|HACK)\b"), "遗留 TODO/FIXME，需跟进"),
    ("medium", "disable_tls", re.compile(r"(?i)(verify\s*=\s*False|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*0)"), "禁用 TLS 校验"),
    ("low", "print_debug", re.compile(r"(?m)^\s*print\s*\("), "生产代码中残留 print 调试"),
]


def scan_text(content: str, path: str = "") -> List[Finding]:
    findings: List[Finding] = []
    lines = content.splitlines()
    for idx, line in enumerate(lines, start=1):
        for severity, rule, pattern, message in RULES:
            if pattern.search(line):
                findings.append(
                    Finding(
                        severity=severity,
                        rule=rule,
                        message=message,
                        line=idx,
                        snippet=line.strip()[:200],
                    )
                )
    # 路径级提示
    norm = path.replace("\\", "/")
    if any(x in norm.lower() for x in [".env", "id_rsa", "credentials", "secrets/"]):
        findings.insert(
            0,
            Finding("high", "sensitive_path", "敏感路径文件，审查时需确认未提交真实密钥", 0, path),
        )
    return findings


def findings_to_dict(findings: List[Finding]) -> Dict[str, Any]:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_f = sorted(findings, key=lambda f: order.get(f.severity, 9))
    counts: Dict[str, int] = {}
    for f in sorted_f:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    risk = "low"
    if counts.get("critical"):
        risk = "critical"
    elif counts.get("high"):
        risk = "high"
    elif counts.get("medium"):
        risk = "medium"
    return {
        "risk_level": risk,
        "counts": counts,
        "findings": [asdict(f) for f in sorted_f[:80]],
        "total": len(sorted_f),
    }


def findings_markdown(findings: List[Finding]) -> str:
    data = findings_to_dict(findings)
    if not data["findings"]:
        return "## 静态扫描\n未发现匹配启发式规则的问题。\n"
    lines = [
        "## 静态扫描（启发式）",
        f"- 风险等级：`{data['risk_level']}`",
        f"- 命中：{data['total']} 条 {data['counts']}",
        "",
    ]
    for f in data["findings"][:40]:
        loc = f"L{f['line']}" if f["line"] else "path"
        lines.append(f"- **[{f['severity']}]** `{f['rule']}` @ {loc} — {f['message']}")
        if f["snippet"]:
            lines.append(f"  `{f['snippet']}`")
    return "\n".join(lines) + "\n"
