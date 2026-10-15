"""代码 RAG：pgvector 优先；无 Postgres 时自动降级到本地 JSON 向量库。

面试点：
- 纯向量对标识符检索弱，BM25 补关键词；RRF 无需校准分数尺度
- RRF 后可选 Transformer Cross-Encoder 精排（Self-Attention 联合编码 query+passage）
- 本地无 Docker 也能演示「索引 → search_codebase」闭环
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except ImportError:  # 兼容旧版 langchain
    from langchain.text_splitter import RecursiveCharacterTextSplitter  # type: ignore

from rank_bm25 import BM25Okapi
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.services.transformer_rerank import transformer_reranker

logger = logging.getLogger(__name__)

CODE_SEPARATORS = [
    "\nclass ",
    "\ndef ",
    "\nasync def ",
    "\nfunction ",
    "\nexport ",
    "\n\n",
    "\n",
    " ",
    "",
]

LOCAL_STORE_PATH = Path(__file__).resolve().parents[2] / ".data" / "rag_local.json"


class RAGService:
    def __init__(self) -> None:
        self._engine: Optional[AsyncEngine] = None
        self._session_factory = None
        self._mode: str = "none"  # pg | local | none
        self._local: Dict[str, List[Dict[str, Any]]] = {}  # repo -> chunks
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=800,
            chunk_overlap=120,
            separators=CODE_SEPARATORS,
            keep_separator=True,
        )
        self._bm25_cache: Dict[str, Tuple[BM25Okapi, List[Dict[str, Any]]]] = {}
        self._load_local_disk()

    @property
    def mode(self) -> str:
        return self._mode

    def _load_local_disk(self) -> None:
        try:
            if LOCAL_STORE_PATH.exists():
                raw = json.loads(LOCAL_STORE_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._local = raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("load local rag store failed: %s", exc)
            self._local = {}

    def _save_local_disk(self) -> None:
        LOCAL_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_STORE_PATH.write_text(
            json.dumps(self._local, ensure_ascii=False),
            encoding="utf-8",
        )

    async def connect(self) -> None:
        """优先 Postgres；失败则切本地模式（始终可索引/检索）。"""
        if self._mode == "pg" and self._engine is not None:
            return
        if self._mode == "local":
            return

        settings = get_settings()
        try:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            self._engine = engine
            self._session_factory = sessionmaker(
                self._engine, class_=AsyncSession, expire_on_commit=False
            )
            self._mode = "pg"
            logger.info("RAG backend: postgres/pgvector")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Postgres unavailable, RAG using local JSON store: %s", exc)
            if self._engine:
                try:
                    await self._engine.dispose()
                except Exception:  # noqa: BLE001
                    pass
            self._engine = None
            self._session_factory = None
            self._mode = "local"
            self._load_local_disk()

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
        self._mode = "none"

    async def ping(self) -> bool:
        """RAG 是否可用（含本地降级）。"""
        try:
            await self.connect()
            return self._mode in {"pg", "local"}
        except Exception:  # noqa: BLE001
            return False

    async def postgres_alive(self) -> bool:
        try:
            await self.connect()
            return self._mode == "pg"
        except Exception:  # noqa: BLE001
            return False

    def _embed_sync(self, texts: List[str]) -> List[List[float]]:
        settings = get_settings()
        base = (settings.llm_base_url or "").lower()
        no_remote_emb = (
            not settings.llm_api_key
            or not settings.embedding_model
            or "deepseek" in base
        )
        if no_remote_emb:
            return [self._hash_embed(t) for t in texts]
        try:
            from langchain_openai import OpenAIEmbeddings

            emb = OpenAIEmbeddings(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.embedding_model,
            )
            return emb.embed_documents(texts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding API failed, fallback to hash vectors: %s", exc)
            return [self._hash_embed(t) for t in texts]

    def _hash_embed(self, text_value: str, dim: int = 256) -> List[float]:
        # 本地模式用较小维度，JSON 更轻；pg 模式仍可 1536
        import hashlib

        vec = [0.0] * dim
        tokens = text_value.lower().split() or ["_empty_"]
        for tok in tokens:
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        return sum(a[i] * b[i] for i in range(n))

    async def index_file(self, repo: str, path: str, content: str) -> Dict[str, Any]:
        """索引前先走 data_clean，返回 chunks 数与清洗统计。"""
        from app.services.data_clean import clean_document

        await self.connect()
        cleaned = clean_document(path, content)
        path = cleaned.path
        if not cleaned.indexable:
            return {
                "chunks": 0,
                "path": path,
                "skipped": True,
                "clean": cleaned.stats.to_dict(),
            }

        chunks = self._splitter.split_text(cleaned.content)
        if not chunks:
            return {
                "chunks": 0,
                "path": path,
                "skipped": True,
                "clean": cleaned.stats.to_dict(),
            }
        embeddings = self._embed_sync(chunks)

        if self._mode == "local":
            existing = [c for c in self._local.get(repo, []) if c.get("path") != path]
            now = int(time.time())
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                existing.append(
                    {
                        "id": f"{repo}:{path}:{i}",
                        "path": path,
                        "chunk_index": i,
                        "content": chunk,
                        "embedding": emb,
                        "updated_at": now,
                    }
                )
            self._local[repo] = existing
            self._save_local_disk()
            self._bm25_cache.pop(repo, None)
            return {
                "chunks": len(chunks),
                "path": path,
                "skipped": False,
                "clean": cleaned.stats.to_dict(),
            }

        # pg path
        async with self._session_factory() as session:
            await session.execute(
                text("DELETE FROM code_chunks WHERE repo = :repo AND path = :path"),
                {"repo": repo, "path": path},
            )
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                # pgvector 表多为 1536 维；本地 hash 可能较短，右侧补零
                if len(emb) < 1536:
                    emb = emb + [0.0] * (1536 - len(emb))
                emb_literal = "[" + ",".join(str(float(x)) for x in emb[:1536]) + "]"
                await session.execute(
                    text(
                        """
                        INSERT INTO code_chunks (repo, path, chunk_index, content, embedding, metadata)
                        VALUES (:repo, :path, :idx, :content, CAST(:emb AS vector), CAST(:meta AS jsonb))
                        ON CONFLICT (repo, path, chunk_index)
                        DO UPDATE SET content = EXCLUDED.content, embedding = EXCLUDED.embedding, updated_at = NOW()
                        """
                    ),
                    {
                        "repo": repo,
                        "path": path,
                        "idx": i,
                        "content": chunk,
                        "emb": emb_literal,
                        "meta": f'{{"chars": {len(chunk)}}}',
                    },
                )
            await session.commit()
        self._bm25_cache.pop(repo, None)
        return {
            "chunks": len(chunks),
            "path": path,
            "skipped": False,
            "clean": cleaned.stats.to_dict(),
        }

    async def _load_corpus(self, repo: str) -> List[Dict[str, Any]]:
        await self.connect()
        if self._mode == "local":
            return list(self._local.get(repo, []))

        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT id, path, chunk_index, content FROM code_chunks WHERE repo = :repo"
                ),
                {"repo": repo},
            )
            rows = result.mappings().all()
        return [dict(r) for r in rows]

    def _get_bm25(self, repo: str, docs: List[Dict[str, Any]]) -> BM25Okapi:
        if repo in self._bm25_cache and len(self._bm25_cache[repo][1]) == len(docs):
            return self._bm25_cache[repo][0]
        tokenized = [d["content"].lower().split() for d in docs]
        bm25 = BM25Okapi(tokenized) if tokenized else BM25Okapi([["empty"]])
        self._bm25_cache[repo] = (bm25, docs)
        return bm25

    @staticmethod
    def rrf_fuse(
        ranked_lists: List[List[int]],
        k: int = 60,
        top_n: int = 8,
    ) -> List[int]:
        scores: Dict[int, float] = {}
        for ranked in ranked_lists:
            for rank, doc_id in enumerate(ranked):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: -x[1])[:top_n]]

    async def hybrid_search(
        self,
        repo: str,
        query: str,
        top_k: int = 8,
        rerank: bool = False,
    ) -> List[Dict[str, Any]]:
        docs = await self._load_corpus(repo)
        if not docs:
            return []

        bm25 = self._get_bm25(repo, docs)
        bm25_scores = bm25.get_scores(query.lower().split())
        bm25_ranked = sorted(range(len(docs)), key=lambda i: -bm25_scores[i])

        q_emb = self._embed_sync([query])[0]

        if self._mode == "local":
            scored = []
            for i, d in enumerate(docs):
                emb = d.get("embedding") or []
                scored.append((i, self._cosine(q_emb, emb)))
            scored.sort(key=lambda x: -x[1])
            vec_ranked = [i for i, _ in scored[: top_k * 3]]
        else:
            if len(q_emb) < 1536:
                q_emb = q_emb + [0.0] * (1536 - len(q_emb))
            emb_literal = "[" + ",".join(str(float(x)) for x in q_emb[:1536]) + "]"
            async with self._session_factory() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT id, path, chunk_index, content,
                               1 - (embedding <=> CAST(:emb AS vector)) AS score
                        FROM code_chunks
                        WHERE repo = :repo AND embedding IS NOT NULL
                        ORDER BY embedding <=> CAST(:emb AS vector)
                        LIMIT :lim
                        """
                    ),
                    {"repo": repo, "emb": emb_literal, "lim": top_k * 3},
                )
                vec_rows = result.mappings().all()
            id_to_idx = {d["id"]: i for i, d in enumerate(docs)}
            vec_ranked = [id_to_idx[r["id"]] for r in vec_rows if r["id"] in id_to_idx]

        fused_idx = self.rrf_fuse([bm25_ranked[: top_k * 3], vec_ranked], top_n=top_k)
        results = []
        for i in fused_idx:
            d = docs[i]
            results.append(
                {
                    "path": d["path"],
                    "chunk_index": d["chunk_index"],
                    "content": d["content"][:2000],
                    "score": float(bm25_scores[i]),
                }
            )

        if rerank and results:
            settings = get_settings()
            results = transformer_reranker.rerank(
                query,
                results,
                enabled=settings.transformer_rerank_enabled,
                model_name=settings.transformer_rerank_model,
                top_k=top_k,
            )
        return results

    async def search_codebase(self, repo: str, query: str, top_k: int = 5) -> str:
        hits = await self.hybrid_search(repo, query, top_k=top_k, rerank=True)
        if not hits:
            return "未找到相关代码块。请先在「仓库」页点「索引 README→RAG」，或换个查询词。"
        rerank_mode = (hits[0].get("rerank") if hits else None) or transformer_reranker.mode
        mode = f"（后端: {self._mode} · rerank: {rerank_mode}）"
        parts = [f"_RAG {mode}_"]
        for h in hits:
            parts.append(f"### {h['path']}#{h['chunk_index']}\n{h['content']}")
        return "\n\n".join(parts)


rag_service = RAGService()
