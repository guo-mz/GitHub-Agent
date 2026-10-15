"""烟雾：repos router 可被单体 / repo_svc 挂载。"""

from app.api import repos
from app.main import app as monolith


def test_repos_router_has_routes():
    paths = {getattr(r, "path", "") for r in repos.router.routes}
    assert any("/api/repos/{owner}/{repo}/tree" in p for p in paths)
    assert any("ai-edit" in p for p in paths)


def test_monolith_includes_repos():
    paths = []
    for r in monolith.routes:
        p = getattr(r, "path", None)
        if p:
            paths.append(p)
    assert any("/api/repos/{owner}/{repo}/tree" == p for p in paths)
    assert "/health" in paths
