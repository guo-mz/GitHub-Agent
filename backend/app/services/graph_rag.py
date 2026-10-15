"""GraphRAG：代码依赖图谱 + 混合检索融合。

面试点：
- 构图层：AST/正则抽 import、defines、calls
- 检索层：种子节点 → 多跳邻居 → 相关文件 → 与 BM25/向量 RRF 结果并集
- 存储：本地 JSON 图（可换成 Neo4j，语义不变）
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.code_graph_extract import extract_file, is_graphable
from app.services.graph_store import graph_store
from app.services.rag_service import rag_service

logger = logging.getLogger(__name__)


class GraphRAGService:
    def index_source(self, repo: str, path: str, content: str) -> Dict[str, Any]:
        if not is_graphable(path):
            return {"ok": True, "skipped": True, "reason": "not graphable", "path": path}
        delta = extract_file(path, content)
        data = graph_store.merge_delta(repo, delta.nodes, delta.edges, path=path)
        return {
            "ok": True,
            "skipped": False,
            "path": path,
            "nodes_added": len(delta.nodes),
            "edges_added": len(delta.edges),
            "stats": data.get("stats") or {},
        }

    def summary(self, repo: str) -> Dict[str, Any]:
        return graph_store.summary(repo)

    def viz(self, repo: str, *, max_nodes: int = 80) -> Dict[str, Any]:
        return graph_store.viz(repo, max_nodes=max_nodes)

    def _seed_nodes(self, data: Dict[str, Any], query: str, limit: int = 12) -> List[str]:
        q = query.lower().strip()
        tokens = [t for t in q.replace("/", " ").replace(".", " ").split() if len(t) >= 2]
        if not tokens:
            tokens = [q] if q else []
        scored: List[Tuple[float, str]] = []
        for nid, meta in (data.get("nodes") or {}).items():
            blob = " ".join(
                str(meta.get(k) or "")
                for k in ("id", "path", "name", "type", "lang")
            ).lower()
            hit = sum(1 for t in tokens if t in blob or t in nid.lower())
            if hit:
                # 文件/符号加权
                bonus = 1.5 if meta.get("type") in {"file", "function", "class"} else 1.0
                scored.append((hit * bonus, nid))
        scored.sort(key=lambda x: -x[0])
        return [nid for _, nid in scored[:limit]]

    def expand(
        self,
        repo: str,
        seed_ids: List[str],
        *,
        hops: int = 2,
        rels: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        data = graph_store.load(repo)
        nodes = data.get("nodes") or {}
        edges = data.get("edges") or []
        allow = rels or {
            "imports",
            "imports_file",
            "imports_symbol",
            "defines",
            "calls",
        }
        adj: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
        for e in edges:
            if e.get("rel") not in allow:
                continue
            adj[e["src"]].append((e["dst"], e["rel"]))
            # 反向边便于「谁依赖我」
            adj[e["dst"]].append((e["src"], "rev:" + e["rel"]))

        visited: Set[str] = set()
        trail: List[Dict[str, str]] = []
        q: deque[Tuple[str, int]] = deque((s, 0) for s in seed_ids if s in nodes or True)
        while q:
            nid, dist = q.popleft()
            if nid in visited or dist > hops:
                continue
            visited.add(nid)
            for nxt, rel in adj.get(nid, []):
                if nxt not in visited:
                    trail.append({"from": nid, "to": nxt, "rel": rel})
                    q.append((nxt, dist + 1))

        files: List[str] = []
        for nid in visited:
            meta = nodes.get(nid) or {}
            if meta.get("type") == "file" and meta.get("path"):
                files.append(meta["path"])
            elif meta.get("path") and str(nid).startswith("symbol:"):
                files.append(meta["path"])
            elif str(nid).startswith("file:"):
                files.append(nid[5:])
        # 去重保序
        seen: Set[str] = set()
        uniq_files = []
        for f in files:
            if f in seen:
                continue
            # 丢掉明显不存在的猜测路径噪声（可选保留）
            seen.add(f)
            uniq_files.append(f)

        return {
            "seeds": seed_ids,
            "visited_nodes": list(visited)[:80],
            "trail": trail[:40],
            "files": uniq_files[:30],
            "stats": data.get("stats") or {},
        }

    def query_dependencies(self, repo: str, query: str, hops: int = 2) -> Dict[str, Any]:
        data = graph_store.load(repo)
        if not (data.get("nodes") or {}):
            return {
                "ok": False,
                "error": "图谱为空。请先在仓库页点「构建代码图谱(GraphRAG)」或索引 .py/.ts 文件。",
                "files": [],
            }
        seeds = self._seed_nodes(data, query)
        if not seeds:
            # 回退：用 query 当路径片段
            for nid, meta in (data.get("nodes") or {}).items():
                if meta.get("type") == "file" and query.lower() in (meta.get("path") or "").lower():
                    seeds.append(nid)
                    if len(seeds) >= 5:
                        break
        expanded = self.expand(repo, seeds, hops=hops)
        return {"ok": True, "query": query, **expanded}

    async def graph_search(
        self,
        repo: str,
        query: str,
        top_k: int = 5,
        hops: int = 2,
    ) -> Dict[str, Any]:
        """图谱扩展文件集合 + 混合检索，返回可给 LLM 的上下文。"""
        dep = self.query_dependencies(repo, query, hops=hops)
        graph_files = set(dep.get("files") or [])

        hits = await rag_service.hybrid_search(repo, query, top_k=top_k * 2, rerank=True)
        # 图谱命中文件的 chunk 优先
        preferred = [h for h in hits if h.get("path") in graph_files]
        others = [h for h in hits if h.get("path") not in graph_files]
        merged = (preferred + others)[:top_k]

        # 若向量库几乎没有，仍返回图谱结构供问答
        return {
            "ok": bool(dep.get("ok")),
            "mode": "graphrag",
            "query": query,
            "graph": {
                "seeds": dep.get("seeds") or [],
                "files": dep.get("files") or [],
                "trail": dep.get("trail") or [],
                "stats": dep.get("stats") or {},
            },
            "chunks": merged,
            "error": dep.get("error"),
        }

    async def format_answer(self, repo: str, query: str, top_k: int = 5) -> str:
        result = await self.graph_search(repo, query, top_k=top_k)
        if result.get("error") and not result.get("graph", {}).get("files"):
            return str(result["error"])
        g = result.get("graph") or {}
        parts = [
            "_GraphRAG（图谱增强检索）_",
            f"种子节点：{', '.join((g.get('seeds') or [])[:6]) or '（关键词弱匹配）'}",
            f"关联文件（{len(g.get('files') or [])}）："
            + ", ".join((g.get("files") or [])[:12]),
        ]
        trail = g.get("trail") or []
        if trail:
            parts.append("依赖路径样例：")
            for t in trail[:8]:
                parts.append(f"- `{t.get('from')}` —{t.get('rel')}→ `{t.get('to')}`")
        chunks = result.get("chunks") or []
        if chunks:
            parts.append("\n相关代码块：")
            for h in chunks:
                parts.append(f"### {h['path']}#{h.get('chunk_index', 0)}\n{h.get('content', '')}")
        elif not trail:
            parts.append("图谱有节点但未扩到边；可换更具体的模块/文件名再问。")
        return "\n".join(parts)


graph_rag_service = GraphRAGService()
