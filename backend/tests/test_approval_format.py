from app.services.approval_format import format_pending_diff


def test_format_create_pr_readable():
    text = format_pending_diff(
        [
            {
                "name": "create_pr",
                "args": {
                    "title": "fix readme",
                    "head": "agent/edit-1",
                    "base": "main",
                    "body": "auto",
                },
            }
        ],
        changed_files=["README.md"],
        repo="guo-mz/-",
    )
    assert "仓库: guo-mz/-" in text
    assert "README.md" in text
    assert "create_pr" in text
    assert "agent/edit-1 → main" in text
    assert "```json" not in text
