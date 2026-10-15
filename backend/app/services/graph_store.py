"""代码知识图谱本地持久化（JSON）—— 无 Neo4j 也能演示 GraphRAG。"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

GRAPH_DIR = Path(__file__).resolve().parents[2] / ".data" / "code_graphs"


def _safe_repo(repo: str) -> str:
    return re.sub(r"[^\w.-]+", "_", repo.strip()) or "repo"


class CodeGraphStore:
    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}

    def _path(self, repo: str) -> Path:
        GRAPH_DIR.mkdir(parents=True, exist_ok=True)
        return GRAPH_DIR / f"{_safe_repo(repo)}.json"

    def load(self, repo: str) -> Dict[str, Any]:
        if repo in self._cache:
            return self._cache[repo]
        p = self._path(repo)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cache[repo] = data
                    return data
            except Exception as exc:  # noqa: BLE001
                logger.warning("load graph failed %s: %s", repo, exc)
        empty = {
            "repo": repo,
            "nodes": {},
            "edges": [],
            "updated_at": 0,
            "stats": {"nodes": 0, "edges": 0, "files": 0},
        }
        self._cache[repo] = empty
        return empty

    def save(self, repo: str, data: Dict[str, Any]) -> None:
        data["repo"] = repo
        data["updated_at"] = int(time.time())
        nodes = data.get("nodes") or {}
        edges = data.get("edges") or []
        files = sum(1 for n in nodes.values() if n.get("type") == "file")
        data["stats"] = {"nodes": len(nodes), "edges": len(edges), "files": files}
        self._path(repo).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self._cache[repo] = data

    def merge_delta(self, repo: str, nodes: Dict[str, Any], edges: List[Dict[str, str]], *, path: str) -> Dict[str, Any]:
        """用单文件增量替换该文件相关旧边，再合并。"""
        data = self.load(repo)
        old_nodes: Dict[str, Any] = dict(data.get("nodes") or {})
        old_edges: List[Dict[str, str]] = list(data.get("edges") or [])
        fid = f"file:{path.replace(chr(92), '/')}"

        # 删掉以该文件为 src、或节点 path 等于该文件的旧边/符号
        drop_ids: Set[str] = {fid}
        for nid, meta in list(old_nodes.items()):
            if meta.get("path") == path or nid.startswith(f"symbol:{path}"):
                drop_ids.add(nid)
        old_nodes = {k: v for k, v in old_nodes.items() if k not in drop_ids}
        old_edges = [
            e
            for e in old_edges
            if e.get("src") not in drop_ids and e.get("dst") not in drop_ids
        ]

        old_nodes.update(nodes)
        # 去重边
        seen = {(e.get("src"), e.get("dst"), e.get("rel")) for e in old_edges}
        for e in edges:
            key = (e.get("src"), e.get("dst"), e.get("rel"))
            if key in seen:
                continue
            seen.add(key)
            old_edges.append(e)

        data["nodes"] = old_nodes
        data["edges"] = old_edges
        self.save(repo, data)
        return data

    def summary(self, repo: str) -> Dict[str, Any]:
        data = self.load(repo)
        return {
            "repo": repo,
            "stats": data.get("stats") or {},
            "updated_at": data.get("updated_at") or 0,
            "sample_edges": (data.get("edges") or [])[:12],
        }

    def viz(
        self,
        repo: str,
        *,
        max_nodes: int = 80,
        prefer_files: bool = True,
    ) -> Dict[str, Any]:
        """前端可视化载荷：优先文件节点 + 依赖边，控制规模以免画布爆炸。"""
        data = self.load(repo)
        all_nodes: Dict[str, Any] = dict(data.get("nodes") or {})
        all_edges: List[Dict[str, str]] = list(data.get("edges") or [])

        # 优先 file / import 边，便于演示「模块依赖」
        file_ids = [nid for nid, m in all_nodes.items() if m.get("type") == "file"]
        if prefer_files and file_ids:
            chosen: List[str] = file_ids[:max_nodes]
        else:
            # 按度数挑热点
            deg: Dict[str, int] = {nid: 0 for nid in all_nodes}
            for e in all_edges:
                s, d = e.get("src") or "", e.get("dst") or ""
                if s in deg:
                    deg[s] += 1
                if d in deg:
                    deg[d] += 1
            chosen = [nid for nid, _ in sorted(deg.items(), key=lambda x: -x[1])[:max_nodes]]

        chosen_set = set(chosen)
        # 补上边上另一端（若仍有空位）
        for e in all_edges:
            if len(chosen_set) >= max_nodes:
                break
            s, d = e.get("src") or "", e.get("dst") or ""
            if s in chosen_set and d in all_nodes:
                chosen_set.add(d)
            if d in chosen_set and s in all_nodes:
                chosen_set.add(s)
        chosen_set = set(list(chosen_set)[:max_nodes])

        nodes_out = []
        for nid in chosen_set:
            meta = all_nodes.get(nid) or {}
            label = str(meta.get("name") or meta.get("path") or nid)
            if len(label) > 36:
                label = "…" + label[-34:]
            nodes_out.append(
                {
                    "id": nid,
                    "label": label,
                    "type": meta.get("type") or "node",
                    "path": meta.get("path") or "",
                }
            )

        edges_out = []
        for e in all_edges:
            s, d = e.get("src") or "", e.get("dst") or ""
            if s in chosen_set and d in chosen_set:
                edges_out.append(
                    {
                        "source": s,
                        "target": d,
                        "rel": e.get("rel") or "depends",
                    }
                )
            if len(edges_out) >= max_nodes * 3:
                break

        return {
            "repo": repo,
            "stats": data.get("stats") or {},
            "updated_at": data.get("updated_at") or 0,
            "nodes": nodes_out,
            "edges": edges_out,
            "truncated": len(all_nodes) > len(nodes_out) or len(all_edges) > len(edges_out),
        }


graph_store = CodeGraphStore()
