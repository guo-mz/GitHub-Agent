"""从源码抽取模块/符号依赖边 —— GraphRAG 构图层。

支持：Python(ast) 的 import / 函数类定义 / 简单调用；
JS/TS/Vue 脚本的 import/require（正则）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class GraphDelta:
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edges: List[Dict[str, str]] = field(default_factory=list)

    def add_node(self, nid: str, **meta: Any) -> None:
        cur = self.nodes.get(nid) or {}
        cur.update({k: v for k, v in meta.items() if v is not None})
        cur.setdefault("id", nid)
        self.nodes[nid] = cur

    def add_edge(self, src: str, dst: str, rel: str) -> None:
        if not src or not dst or src == dst:
            return
        self.edges.append({"src": src, "dst": dst, "rel": rel})


def _file_id(path: str) -> str:
    return f"file:{path.replace('\\', '/')}"


def _mod_id(name: str) -> str:
    return f"module:{name}"


def _sym_id(file_path: str, name: str) -> str:
    return f"symbol:{file_path.replace('\\', '/')}::{name}"


def _resolve_py_import(module: str, level: int, file_path: str) -> Optional[str]:
    """相对 import 尽量解析成仓库内路径猜测（.py）。"""
    if level and level > 0:
        parts = Path(file_path).with_suffix("").parts
        base = list(parts[:-level] if level <= len(parts) else [])
        if module:
            base.extend(module.split("."))
        if not base:
            return None
        return "/".join(base) + ".py"
    if not module:
        return None
    # 绝对包名 → 路径猜测
    return module.replace(".", "/") + ".py"


def extract_python(path: str, content: str) -> GraphDelta:
    g = GraphDelta()
    fid = _file_id(path)
    g.add_node(fid, type="file", path=path, lang="python")
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return g

    defined: Set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sid = _sym_id(path, node.name)
            defined.add(node.name)
            g.add_node(sid, type="function", name=node.name, path=path)
            g.add_edge(fid, sid, "defines")
        elif isinstance(node, ast.ClassDef):
            sid = _sym_id(path, node.name)
            defined.add(node.name)
            g.add_node(sid, type="class", name=node.name, path=path)
            g.add_edge(fid, sid, "defines")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mid = _sym_id(path, f"{node.name}.{item.name}")
                    g.add_node(mid, type="method", name=f"{node.name}.{item.name}", path=path)
                    g.add_edge(sid, mid, "defines")
                    g.add_edge(fid, mid, "defines")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = (alias.name or "").split(".")[0]
                mid = _mod_id(alias.name)
                g.add_node(mid, type="module", name=alias.name, external=True)
                g.add_edge(fid, mid, "imports")
                # 猜测同仓路径
                guess = alias.name.replace(".", "/") + ".py"
                g.add_edge(fid, _file_id(guess), "imports_file")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            mid = _mod_id(mod or f".{'l'*node.level}")
            g.add_node(mid, type="module", name=mod or ".", external=node.level == 0)
            g.add_edge(fid, mid, "imports")
            resolved = _resolve_py_import(mod, node.level or 0, path)
            if resolved:
                g.add_edge(fid, _file_id(resolved), "imports_file")
            for alias in node.names:
                if alias.name == "*":
                    continue
                # from x import y → 可能用到符号
                g.add_edge(fid, _sym_id(resolved or mod, alias.name), "imports_symbol")

    # 同文件内调用：Name / Attribute
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            if callee and callee in defined:
                # 粗粒度：文件 → 符号 calls
                g.add_edge(fid, _sym_id(path, callee), "calls")

    return g


_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*\s{},]+)\s+from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)|import\(\s*['"]([^'"]+)['"]\s*\))""",
    re.M,
)
_JS_EXPORT_FN = re.compile(
    r"""(?:export\s+(?:async\s+)?function\s+(\w+)|(?:async\s+)?function\s+(\w+)\s*\(|const\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)""",
    re.M,
)


def extract_js_like(path: str, content: str) -> GraphDelta:
    g = GraphDelta()
    fid = _file_id(path)
    lang = "vue" if path.endswith(".vue") else "javascript"
    g.add_node(fid, type="file", path=path, lang=lang)

    # vue：只抽 script 块
    if path.endswith(".vue"):
        m = re.search(r"<script[^>]*>([\s\S]*?)</script>", content, re.I)
        content = m.group(1) if m else content

    for m in _JS_IMPORT_RE.finditer(content):
        spec = m.group(1) or m.group(2) or m.group(3) or ""
        if not spec:
            continue
        mid = _mod_id(spec)
        g.add_node(mid, type="module", name=spec, external=not spec.startswith("."))
        g.add_edge(fid, mid, "imports")
        if spec.startswith("."):
            # 相对路径解析
            base = Path(path).parent
            target = (base / spec).as_posix()
            for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".vue"):
                g.add_edge(fid, _file_id(target + ext if ext else target), "imports_file")

    for m in _JS_EXPORT_FN.finditer(content):
        name = m.group(1) or m.group(2) or m.group(3)
        if not name:
            continue
        sid = _sym_id(path, name)
        g.add_node(sid, type="function", name=name, path=path)
        g.add_edge(fid, sid, "defines")

    return g


def extract_file(path: str, content: str) -> GraphDelta:
    p = path.replace("\\", "/").lower()
    if p.endswith(".py"):
        return extract_python(path.replace("\\", "/"), content)
    if p.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue")):
        return extract_js_like(path.replace("\\", "/"), content)
    # 其它文件：仅登记 file 节点，便于图谱完整
    g = GraphDelta()
    g.add_node(_file_id(path), type="file", path=path.replace("\\", "/"), lang="other")
    return g


GRAPHABLE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".mjs", ".cjs")


def is_graphable(path: str) -> bool:
    return path.replace("\\", "/").lower().endswith(GRAPHABLE_SUFFIXES)
