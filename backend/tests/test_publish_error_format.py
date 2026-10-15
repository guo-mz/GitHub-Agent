"""发布失败错误文案：PAT 403 应可读。"""

from app.agents.publish_agent import format_publish_error


def test_format_pat_403():
    raw = (
        '{"message":"Resource not accessible by personal access token",'
        '"documentation_url":"https://docs.github.com/rest/git/refs#create-a-reference",'
        '"status":"403"}'
    )
    out = format_publish_error(raw)
    assert "GitHub Token 权限不足" in out
    assert "Contents" in out or "repo" in out


def test_format_passthrough():
    assert "boom" in format_publish_error("boom")
