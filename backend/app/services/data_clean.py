"""RAG / 索引前数据清洗模块。

面试点：入库前脱敏 + 去噪，避免密钥进向量库、二进制/压缩噪声污染检索。
流水线：path 规范化 → 跳过不可索引 → 密钥/私钥脱敏 → 空白与噪声清理。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# 不进 RAG 的扩展 / 路径
SKIP_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".lock",
}
SKIP_PATH_PARTS = {
    "node_modules/",
    ".git/",
    "dist/",
    "build/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "vendor/",
}

# 密钥/令牌脱敏（替换为占位符，保留结构便于检索「这里有配置」）
REDACT_PATTERNS: List[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.IGNORECASE,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        "aws_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    (
        "github_pat",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "[REDACTED_API_KEY]",
    ),
    (
        "assignment_secret",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|token|access_key)\b\s*[=:]\s*['\"][^'\"]{8,}['\"]"
        ),
        r"\1=[REDACTED]",
    ),
    (
        "bearer",
        re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
        r"\1[REDACTED_TOKEN]",
    ),
]

# 超长 base64 / data-uri 噪声
NOISE_PATTERNS: List[tuple[str, re.Pattern[str], str]] = [
    (
        "data_uri",
        re.compile(r"data:[a-zA-Z0-9.+/-]+;base64,[A-Za-z0-9+/=\s]{200,}"),
        "[STRIPPED_DATA_URI]",
    ),
    (
        "long_base64",
        re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{400,}={0,2}(?![A-Za-z0-9+/])"),
        "[STRIPPED_BASE64]",
    ),
]


@dataclass
class CleanStats:
    skipped: bool = False
    skip_reason: str = ""
    path_normalized: bool = False
    redacted: List[str] = field(default_factory=list)
    noise_stripped: List[str] = field(default_factory=list)
    chars_before: int = 0
    chars_after: int = 0
    lines_collapsed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CleanResult:
    path: str
    content: str
    stats: CleanStats
    indexable: bool = True


def clean_path(path: str) -> str:
    """统一路径：反斜杠→/，去掉前导 ./，折叠多余斜杠。"""
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/+", "/", p)
    return p.lstrip("/")


def _should_skip(path: str, content: str) -> Optional[str]:
    lower = path.lower()
    for part in SKIP_PATH_PARTS:
        if part in lower:
            return f"path_excluded:{part.strip('/')}"
    for ext in SKIP_EXTENSIONS:
        if lower.endswith(ext):
            return f"ext_excluded:{ext}"
    # 明显二进制：高比例 NUL / 非文本控制字符
    if "\x00" in content[:4096]:
        return "binary_nul"
    sample = content[:2048]
    if sample:
        non_text = sum(1 for c in sample if ord(c) < 9 or (13 < ord(c) < 32))
        if non_text / max(len(sample), 1) > 0.3:
            return "binary_control_chars"
    # 极短无意义
    if not content.strip():
        return "empty"
    return None


def _normalize_whitespace(text: str) -> tuple[str, int]:
    # 统一换行，去掉行尾空白，压缩连续空行
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: List[str] = []
    blank_run = 0
    collapsed = 0
    for ln in lines:
        if ln == "":
            blank_run += 1
            if blank_run <= 2:
                out.append(ln)
            else:
                collapsed += 1
        else:
            blank_run = 0
            out.append(ln)
    return "\n".join(out).strip() + ("\n" if out else ""), collapsed


def clean_document(path: str, content: str) -> CleanResult:
    """对单文件做入库前清洗，返回可索引正文与统计。"""
    stats = CleanStats(chars_before=len(content or ""))
    norm_path = clean_path(path)
    stats.path_normalized = norm_path != (path or "").replace("\\", "/")

    raw = content or ""
    reason = _should_skip(norm_path, raw)
    if reason:
        stats.skipped = True
        stats.skip_reason = reason
        stats.chars_after = 0
        return CleanResult(path=norm_path, content="", stats=stats, indexable=False)

    text = raw

    for name, pattern, repl in REDACT_PATTERNS:
        new_text, n = pattern.subn(repl, text)
        if n:
            stats.redacted.append(f"{name}×{n}")
            text = new_text

    for name, pattern, repl in NOISE_PATTERNS:
        new_text, n = pattern.subn(repl, text)
        if n:
            stats.noise_stripped.append(f"{name}×{n}")
            text = new_text

    text, collapsed = _normalize_whitespace(text)
    stats.lines_collapsed = collapsed
    stats.chars_after = len(text)

    if not text.strip():
        stats.skipped = True
        stats.skip_reason = "empty_after_clean"
        return CleanResult(path=norm_path, content="", stats=stats, indexable=False)

    return CleanResult(path=norm_path, content=text, stats=stats, indexable=True)
