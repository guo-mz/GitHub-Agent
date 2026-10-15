"""Transformer Cross-Encoder 重排（Hybrid RAG 第三段）。

面试点：
- Encoder 对 (query, passage) 联合编码 → 相关性分数（比双塔 Bi-Encoder 更准、更慢）
- Self-Attention 让 query token 与 doc token 互相 attend，适合精排 top-N
- 未安装 sentence-transformers / 未启用时回退关键词计数，不阻塞启动
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def keyword_rerank(query: str, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """无 Transformer 时的轻量回退：词重叠 + BM25 score。"""
    q_terms = set(query.lower().split())
    ranked = sorted(
        results,
        key=lambda r: (
            sum(1 for t in q_terms if t in (r.get("content") or "").lower()),
            float(r.get("score") or 0.0),
        ),
        reverse=True,
    )
    for r in ranked:
        r["rerank"] = "keyword"
    return ranked


class TransformerReranker:
    def __init__(self) -> None:
        self._model = None
        self._model_name: str = ""
        self._load_error: Optional[str] = None
        self._tried = False

    @property
    def mode(self) -> str:
        if self._model is not None:
            return "transformer"
        if self._load_error:
            return f"keyword({self._load_error[:40]})"
        return "keyword"

    def health_label(self, *, enabled: bool) -> str:
        """侧栏 /health 用：transformer | keyword | off（不强制加载模型）。"""
        if not enabled:
            return "off"
        if self._model is not None:
            return "transformer"
        if self._load_error:
            return "keyword"
        try:
            import sentence_transformers  # noqa: F401

            return "transformer"
        except Exception:  # noqa: BLE001
            return "keyword"

    def _ensure_model(self, model_name: str) -> bool:
        if self._model is not None and self._model_name == model_name:
            return True
        if self._tried and self._model_name == model_name:
            return False
        self._tried = True
        self._model_name = model_name
        try:
            from sentence_transformers import CrossEncoder  # type: ignore

            self._model = CrossEncoder(model_name)
            self._load_error = None
            logger.info("Transformer CrossEncoder loaded: %s", model_name)
            return True
        except Exception as exc:  # noqa: BLE001
            self._model = None
            self._load_error = str(exc)[:200]
            logger.warning("Transformer rerank unavailable, keyword fallback: %s", exc)
            return False

    def rerank(
        self,
        query: str,
        results: List[Dict[str, Any]],
        *,
        enabled: bool,
        model_name: str,
        top_k: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not results:
            return results
        if not enabled or not self._ensure_model(model_name):
            out = keyword_rerank(query, results)
            return out[:top_k] if top_k else out

        pairs = [(query, (r.get("content") or "")[:2000]) for r in results]
        try:
            scores = self._model.predict(pairs)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.warning("CrossEncoder predict failed: %s", exc)
            out = keyword_rerank(query, results)
            return out[:top_k] if top_k else out

        scored = list(zip(results, scores))
        scored.sort(key=lambda x: float(x[1]), reverse=True)
        out: List[Dict[str, Any]] = []
        for r, s in scored:
            item = dict(r)
            item["score"] = float(s)
            item["rerank"] = "transformer"
            item["rerank_model"] = self._model_name
            out.append(item)
        return out[:top_k] if top_k else out


transformer_reranker = TransformerReranker()
