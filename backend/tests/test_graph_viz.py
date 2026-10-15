"""图谱可视化载荷单测。"""

from app.services.graph_store import CodeGraphStore


def test_viz_prefers_file_nodes(tmp_path, monkeypatch):
    store = CodeGraphStore()
    monkeypatch.setattr(
        "app.services.graph_store.GRAPH_DIR",
        tmp_path,
    )
    repo = "demo/repo"
    store.merge_delta(
        repo,
        {
            "file:app/a.py": {"type": "file", "path": "app/a.py", "name": "a.py"},
            "file:app/b.py": {"type": "file", "path": "app/b.py", "name": "b.py"},
            "symbol:app/a.py:foo": {"type": "function", "path": "app/a.py", "name": "foo"},
        },
        [
            {"src": "file:app/a.py", "dst": "file:app/b.py", "rel": "imports"},
            {"src": "file:app/a.py", "dst": "symbol:app/a.py:foo", "rel": "defines"},
        ],
        path="app/a.py",
    )
    viz = store.viz(repo, max_nodes=10)
    assert viz["stats"]["nodes"] >= 2
    ids = {n["id"] for n in viz["nodes"]}
    assert "file:app/a.py" in ids
    assert "file:app/b.py" in ids
    assert any(e["source"] == "file:app/a.py" and e["target"] == "file:app/b.py" for e in viz["edges"])


def test_rerank_health_label():
    from app.services.transformer_rerank import TransformerReranker

    r = TransformerReranker()
    assert r.health_label(enabled=False) == "off"
    label = r.health_label(enabled=True)
    assert label in {"transformer", "keyword"}
