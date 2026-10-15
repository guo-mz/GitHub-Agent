"""数据清洗单测 —— 面试可指：密钥脱敏、跳过二进制、路径规范化。"""

from app.services.data_clean import clean_document, clean_path


def test_clean_path_normalize():
    assert clean_path(".\\src\\\\app.py") == "src/app.py"
    assert clean_path("./README.md") == "README.md"


def test_skip_binary_and_node_modules():
    r = clean_document("assets/logo.png", "fakepng")
    assert r.indexable is False
    assert "ext_excluded" in r.stats.skip_reason

    r2 = clean_document("node_modules/ lodash/index.js", "module.exports=1")
    assert r2.indexable is False


def test_redact_secrets_and_keys():
    raw = (
        "api_key = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n"
        "AWS = AKIAIOSFODNN7EXAMPLE\n"
        "token = 'ghp_abcdefghijklmnopqrstuvwxyz012345'\n"
        "hello world\n"
    )
    r = clean_document("config.py", raw)
    assert r.indexable is True
    assert "sk-abcdef" not in r.content
    assert "AKIAIOSFODNN7EXAMPLE" not in r.content
    assert "ghp_" not in r.content
    assert "[REDACTED" in r.content
    assert r.stats.redacted


def test_strip_long_base64_noise():
    blob = "A" * 500
    raw = f"const img = '{blob}';\nexport const ok = 1;\n"
    r = clean_document("src/x.js", raw)
    assert r.indexable is True
    assert blob not in r.content
    assert "[STRIPPED_BASE64]" in r.content


def test_whitespace_collapse():
    raw = "a\n\n\n\n\nb\n"
    r = clean_document("a.txt", raw)
    assert "\n\n\n\n" not in r.content
    assert r.stats.lines_collapsed >= 1
