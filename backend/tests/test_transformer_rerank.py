"""Transformer Cross-Encoder / 关键词回退。"""

from __future__ import annotations

from app.services.transformer_rerank import TransformerReranker, keyword_rerank


def test_keyword_rerank_prefers_overlap():
    docs = [
        {"content": "unrelated text about cats", "score": 0.9, "path": "a.py"},
        {"content": "deploy key write permission github", "score": 0.1, "path": "b.py"},
    ]
    out = keyword_rerank("deploy key github", docs)
    assert out[0]["path"] == "b.py"
    assert out[0]["rerank"] == "keyword"


def test_rerank_falls_back_when_disabled():
    rr = TransformerReranker()
    docs = [
        {"content": "alpha beta", "score": 1.0, "path": "1"},
        {"content": "query token match here", "score": 0.2, "path": "2"},
    ]
    out = rr.rerank(
        "query token",
        docs,
        enabled=False,
        model_name="cross-encoder/ms-marco-MiniLM-L-6-v2",
    )
    assert out[0]["path"] == "2"
    assert out[0]["rerank"] == "keyword"


def test_rerank_with_mock_cross_encoder():
    rr = TransformerReranker()

    class FakeCE:
        def predict(self, pairs):
            # second doc more relevant
            return [0.1, 0.9]

    rr._model = FakeCE()
    rr._model_name = "fake"
    rr._tried = True
    docs = [
        {"content": "aaa", "score": 1.0, "path": "low"},
        {"content": "bbb", "score": 0.1, "path": "high"},
    ]
    out = rr.rerank("q", docs, enabled=True, model_name="fake", top_k=1)
    assert len(out) == 1
    assert out[0]["path"] == "high"
    assert out[0]["rerank"] == "transformer"
