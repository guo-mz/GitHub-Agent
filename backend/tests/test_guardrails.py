"""护栏单测 —— 面试可指：禁写 main / 敏感路径 / 高危强制审批。"""

from app.services.guardrails import (
    check_branch_isolation,
    check_path_whitelist,
    run_guardrails,
)


def test_block_main_branch():
    assert check_branch_isolation("main")
    assert check_branch_isolation("master")
    assert check_branch_isolation("release") is not None
    assert check_branch_isolation("agent/edit-readme") is None


def test_block_sensitive_paths():
    assert check_path_whitelist([".env"])
    assert check_path_whitelist(["config/.env"])
    assert check_path_whitelist([".github/workflows/ci.yml"])
    assert check_path_whitelist(["Dockerfile"])
    assert check_path_whitelist(["README.md"]) is None
    assert check_path_whitelist(["src/app.py"]) is None


def test_normalize_does_not_strip_dotenv_name():
    """回归：str.lstrip('./') 会把 '.env' 剥成 'env'，导致黑名单失效。"""
    from app.services.guardrails import _normalize_path, _path_blocked

    assert _normalize_path(".env") == ".env"
    assert _path_blocked(".env") is True
    assert _path_blocked("./Dockerfile") is True


def test_update_file_on_main_blocked():
    r = run_guardrails(
        "update_file",
        {"path": "README.md", "branch": "main", "content": "x"},
    )
    assert r.blocked is True
    assert r.passed is False
    assert any("受保护分支" in x for x in r.reasons)


def test_create_pr_needs_approval():
    r = run_guardrails(
        "create_pr",
        {"title": "demo", "head": "agent/fix", "base": "main", "body": "hi"},
    )
    assert r.blocked is False
    assert r.need_approval is True
    assert r.risk_level in {"high", "critical"}


def test_merge_pr_critical():
    r = run_guardrails("merge_pr", {"number": 1, "merge_method": "squash"})
    assert r.need_approval is True
    assert r.risk_level == "critical"
