"""GraphRAG：抽取 + 依赖扩展单测。"""

from app.services.code_graph_extract import extract_python, is_graphable
from app.services.graph_rag import GraphRAGService
from app.services.graph_store import CodeGraphStore


def test_is_graphable():
    assert is_graphable("app/main.py")
    assert is_graphable("src/foo.ts")
    assert not is_graphable("README.md")


def test_extract_python_imports_and_defines():
    src = '''
import os
from app.services import rag_service

def helper():
    return 1

def run():
    helper()
'''
    g = extract_python("app/worker.py", src)
    assert any(n.get("type") == "file" for n in g.nodes.values())
    assert any(e.get("rel") == "imports" for e in g.edges)
    assert any(e.get("rel") == "defines" for e in g.edges)
    assert any(e.get("rel") == "calls" for e in g.edges)


def test_graph_expand_finds_related_file(tmp_path, monkeypatch):
    store = CodeGraphStore()
    monkeypatch.setattr("app.services.graph_rag.graph_store", store)
    monkeypatch.setattr("app.services.graph_store.GRAPH_DIR", tmp_path)

    svc = GraphRAGService()
    svc.index_source(
        "demo/r",
        "app/a.py",
        "from app import b\n\ndef use_b():\n    pass\n",
    )
    svc.index_source(
        "demo/r",
        "app/b.py",
        "def core():\n    return 42\n",
    )
    # 手工补一条 imports_file 边（模拟路径解析命中）
    data = store.load("demo/r")
    data["edges"].append(
        {"src": "file:app/a.py", "dst": "file:app/b.py", "rel": "imports_file"}
    )
    store.save("demo/r", data)

    dep = svc.query_dependencies("demo/r", "a.py 依赖", hops=2)
    assert dep["ok"]
    files = dep.get("files") or []
    assert "app/a.py" in files or any("a.py" in f for f in files)
