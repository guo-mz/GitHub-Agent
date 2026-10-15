"""代码库 RAG / GraphRAG 检索工具。"""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.services.graph_rag import graph_rag_service
from app.services.rag_service import rag_service


class SearchCodebaseInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    query: str = Field(..., description="自然语言或符号查询，如 auth middleware JWT")
    top_k: int = Field(default=5, ge=1, le=20)


class SearchCodeGraphInput(BaseModel):
    repo: str = Field(..., description="owner/repo")
    query: str = Field(
        ...,
        description="模块/文件/符号名或依赖问题，如「谁 import 了 rag_service」「agent 依赖哪些模块」",
    )
    top_k: int = Field(default=5, ge=1, le=20)


@tool("search_codebase", args_schema=SearchCodebaseInput)
async def search_codebase(repo: str, query: str, top_k: int = 5) -> str:
    """在已索引的代码库中做混合检索（BM25+向量 RRF）。解释鉴权逻辑、定位实现时优先使用。"""
    return await rag_service.search_codebase(repo, query, top_k=top_k)


@tool("search_code_graph", args_schema=SearchCodeGraphInput)
async def search_code_graph(repo: str, query: str, top_k: int = 5) -> str:
    """GraphRAG 图谱增强检索：跨文件 import/调用/模块依赖问答。
    用户问「谁依赖 X」「调用链」「模块关系」「从 A 到 B 的依赖」时优先使用。
    需先在仓库页构建代码图谱。"""
    return await graph_rag_service.format_answer(repo, query, top_k=top_k)
