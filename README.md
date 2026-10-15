# GitHub Agent — 企业级智能研发助手

用自然语言驱动 GitHub：读 Issue → 搜代码 → 改文件 → 建分支 → Commit → **人工审批** → 提 PR。  
技术栈：Vue 3 · FastAPI · LangGraph · LangChain · Jev（System One 决策）· Redis · pgvector · GraphRAG（可视）· **上下文工程** · **MCP** · **Transformer Cross-Encoder** · Langfuse · Docker Compose · **GitHub Actions CI**。

---

## 架构图（文字版）

```
┌─────────────┐   SSE/HTTPS    ┌──────────────────────────────────────────┐
│  Vue 3 UI   │ ─────────────► │  FastAPI (chat / approval / oauth / hook) │
│ Chat/审批/仓 │ ◄── event ─── │                                          │
└─────────────┘                └──────────────────┬───────────────────────┘
                                                  │
                                       ┌──────────▼──────────┐
                                       │   LangGraph Agent   │
                                       │ classify→planner→   │
                                       │ compact→agent→tools │
                                       │ →guard→HITL→final   │
                                       └──────────┬──────────┘
                          ┌───────────────┬───────┴────────┬─────────────┐
                          ▼               ▼                ▼             ▼
                     GitHub API      Redis (×3)       pgvector       Langfuse
                     Tools           ckpt/限流/幂等    Hybrid+CE精排    Trace
                                                  ▲
                                           MCP Server (stdio/HTTP)
                                           同一套 ALL_TOOLS
```

**关键控制流：**

1. 用户自然语言 → `POST /api/chat/stream`（SSE）
2. `classify_intent` 判定 read / write / qa
3. `planner` → `compact_context`（token 预算裁剪/摘要）→ `agent` ReAct 选工具 → `tools`  
   （节点名不能叫 `plan`，会与 state 字段冲突——面试加分点）
4. `guardrail_check` 六层护栏；`create_pr` / `merge_pr` → `request_approval`（`interrupt`）
5. 前端弹窗 → `POST /api/approval/{tid}` → 从 Redis Checkpointer **resume**
6. 全程 Langfuse trace；GitHub 调用走 Redis 令牌桶

---

## 目录结构

```
github-agent/
├── docker-compose.yml
├── .env.example
├── README.md
├── backend/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── scripts/init_db.sql
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── api/          # chat, approval, webhook, auth
│       ├── agent/        # graph, state, nodes, edges, prompts
│       ├── tools/        # github_tools, rag_tools
│       ├── mcp_server/   # MCP：ALL_TOOLS → stdio / SSE
│       ├── services/     # github, redis, rag, transformer_rerank, guardrails
│       ├── core/         # checkpointer, rate_limit, idempotency, tracing
│       └── models/schemas.py
├── services/mcp_svc/     # MCP SSE :8013
└── frontend/
    ├── package.json
    ├── vite.config.ts
    ├── Dockerfile
    └── src/              # views / components / stores / api
```

---

## 快速启动（Docker，推荐）

> 更完整的说明见 [docs/DOCKER部署.md](docs/DOCKER部署.md)。**不要**把 `.env` 推到 GitHub。

### 1. 准备凭证

```bash
cd github-agent
cp .env.example .env
```

编辑 `.env`，至少填写：

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | OpenAI 兼容 Key（DeepSeek / 通义等） |
| `GITHUB_TOKEN` | PAT，权限含 `repo` |
| `OPERATOR_API_TOKEN` | 自定义操作员密钥（前端登录用） |
| `LANGFUSE_*` | 可选；`docker compose --profile obs up` 后在 http://localhost:3001 注册回填 |

### 2. 一键启动（单体）

```bash
docker compose up -d --build
# 或前台看日志: docker compose up --build
# 本机非 Docker: powershell -File scripts/start-all.ps1
```

| 服务 | 地址 |
|------|------|
| 前端 | http://localhost:5173 |
| 后端 API / Docs | http://localhost:8000/docs |
| Langfuse（需 `--profile obs`） | http://localhost:3001 |
| Redis | localhost:6379 |
| Postgres+pgvector | localhost:5432 |

浏览器打开前端后，填入与 `.env` 相同的 `OPERATOR_API_TOKEN`。

### 2b. 微服务模式（面试加强 / 保留单体）

三进程：`Gateway:8001` → `Agent:8011` + `Repo:8012`，前端仍只打 Gateway。

增强能力（面试可讲）：Redis 服务发现、Agent Redis DB1 / Repo DB0+Postgres 分库、`X-Trace-Id` 跨服务追踪、Gateway 熔断、Agent→Repo GitHub RPC。

```bash
# 本机
powershell -ExecutionPolicy Bypass -File scripts/start-micro.ps1

# 或 Docker（须在 .env 设置 INTERNAL_RPC_TOKEN，>=16 字符随机串；禁止 dev-internal-rpc）
docker compose -f docker-compose.micro.yml up --build
```

| 服务 | 地址 |
|------|------|
| Gateway（BFF） | http://127.0.0.1:8001/health → `mode=micro` + discovery/breakers |
| 注册表调试 | http://127.0.0.1:8001/internal/discovery |
| Agent | http://127.0.0.1:8011/docs |
| Repo + 内部 RPC | http://127.0.0.1:8012/internal/v1/github/call（需 `X-Internal-Token`） |

代码：`services/*`、`core/service_registry.py`、`core/circuit_breaker.py`、`core/trace_context.py`、`services/github_rpc.py`；业务仍共享 `backend/app`。

### 2c. MCP Server（Cursor / Claude）

同一套 `ALL_TOOLS`，对外暴露 MCP：

```bash
# HTTP 调试（health / tools / JSON-RPC）
powershell -ExecutionPolicy Bypass -File scripts/start-mcp.ps1
# → http://127.0.0.1:8013/health

# Cursor：把 scripts/cursor-mcp.example.json 合进 MCP 配置（stdio）
# 或: cd backend && .venv\Scripts\python.exe -m app.mcp_server
```

### 2d. Transformer 精排（可选）

Hybrid RAG：`BM25 + 向量 → RRF → Cross-Encoder`。未装模型时自动关键词回退。

```bash
pip install -r backend/requirements-transformer.txt
# .env: TRANSFORMER_RERANK_ENABLED=true
```

### 3. 本地开发（可选）

```bash
# 后端
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e .
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```

### 4. 验收路径（端到端）

1. 打开前端，填写 `owner/repo`
2. 发送：`列出最近的 open issues`
3. 发送：`读取 xxx 文件并解释鉴权逻辑`（可先 `POST /api/repos/{o}/{r}/index?path=...` 建索引）
4. 发送：`在新分支修复 … 并创建 PR`
5. 前端弹出审批框 → **同意** → GitHub 出现真实 PR
6. 重复同一指令 → 幂等，不重复建 PR
7. Langfuse 查看完整 trace

---

## 核心设计（面试讲点）

### 1. LangGraph 状态机 + Reducer

- `messages` 用 `add_messages` 智能合并，避免并行节点互相覆盖
- `changed_files` 用 `operator.add` 累加
- 节点**只返回增量 dict**，禁止原地改 state
- `recursion_limit=25` + `retry_count` + Redis 轨迹熔断 → 防死循环

### 1.1 上下文工程（进模前压缩）

- Checkpointer **保留完整** `messages`（审批可恢复）
- `compact_context` 节点按 token 预算裁剪；超长则摘要写入 `context_summary`
- 工具观察写入时截断；裁剪保 `tool_calls`↔`ToolMessage` 配对
- 代码：`services/context_engine.py`；配置：`CONTEXT_MAX_TOKENS` 等

### 2. Interrupt 人工审批

- `create_pr` / `merge_pr` 在 Tool 层挂起，进入 `request_approval`
- `langgraph.types.interrupt` 暂停图；状态进 Redis Checkpointer
- 前端 `ApprovalModal` / 审批中心 → `/api/approval/{tid}` → `Command(resume=...)` 恢复
- **拒绝路径不执行写工具** → 无副作用

### 3. 六层护栏

| 层 | 机制 |
|----|------|
| 1 | 禁止写 `main/master/release/production` |
| 2 | 禁止改 `.github/workflows` / `Dockerfile` / `.env` / `secrets/` |
| 3 | 文件数≤5、行数≤200，超限强制审批 |
| 4 | 高危操作 interrupt |
| 5 | Redis `SET NX PX` 幂等锁 |
| 6 | GitHub SHA 乐观锁，409 → 回 `plan` 重试 |

### 4. Redis 三重职责

1. **Checkpointer**：interrupt/resume  
2. **令牌桶（Lua 原子）**：贴近 GitHub 5000/h，防超发  
3. **幂等 + 会话/审批缓存**

### 5. 代码 RAG

- `RecursiveCharacterTextSplitter`（代码分隔符）
- pgvector 存 embedding
- **BM25 + 向量 → RRF → Transformer Cross-Encoder 精排**（未装则关键词回退）
- 工具名：`search_codebase`

### 6. 可观测

- Langfuse：节点路径、工具 IO、耗时
- 结构化日志带 `trace_id`，与 SSE `thread_id` 对齐

### 7. 异常降级

| 失败点 | 降级 |
|--------|------|
| LLM 分类/规划失败 | 规则启发式 / 默认计划 |
| RedisSaver 不可用 | MemorySaver（单机） |
| 无 Embedding Key | 哈希伪向量（本地联调） |
| GitHub 429 | Retry-After + 令牌桶 sleep |

---

## 安全说明（Phase-3）

- **操作员认证**：敏感读/写需 `OPERATOR_API_TOKEN`（`X-Operator-Token` / Bearer）。未配置则 fail-closed。共享 Token **不是** per-user/per-task ACL。
- **禁止** `VITE_OPERATOR_TOKEN`：Vite 变量会进入浏览器包。本机可用 `localStorage.ga_operator_token`；XSS 可窃取，不适合公网。
- **Webhook**：未配置 `GITHUB_WEBHOOK_SECRET` 时默认拒绝；仅本地可显式 `ALLOW_INSECURE_WEBHOOK=true`。
- **发布硬关闭**：无可信隔离测试执行器时，伪造 `passed=True` 也不能授权真实 GitHub 写；上传仅用已验证 `approved_snapshot` 内存字节。
- **公开例外**：`GET /health` 仅返回存活/组件布尔状态，不含 Diff/Token/任务 payload。

## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/stream` | SSE 流式对话（需操作员 Token） |
| POST | `/api/approval/{tid}` | 提交审批并 resume（SSE，需 Token） |
| GET | `/api/approval/pending` | 待审批列表（需 Token） |
| POST | `/api/webhook/github` | Webhook（HMAC；缺 secret 默认拒绝） |
| GET | `/api/auth/github` | OAuth 跳转 |
| GET | `/api/auth/callback` | OAuth 回调 |
| GET | `/api/repos/{owner}/{repo}/tree` | 仓库树（需 Token） |
| POST | `/api/repos/{owner}/{repo}/index` | 单文件写入 RAG（需 Token） |
| GET | `/health` | 健康检查（公开，无敏感 payload） |

---

## 简历可写亮点（建议表述）

- 基于 **LangGraph** 实现可中断、可恢复的 GitHub 研发 Agent，高危写操作强制 HITL
- Redis 统一承载检查点 / Lua 令牌桶 / 幂等锁，保障并发与配额安全
- pgvector 混合检索（BM25+向量 RRF）支撑代码问答
- Vue3 SSE 实时展示 thought/action/observation，审批弹窗闭环
- Docker Compose 一键拉起 Backend/Frontend/Redis/Postgres/Langfuse

---

## License

MIT — 仅供学习与面试演示；连接真实仓库前请确认 Token 权限与护栏策略。

---

## 延伸文档

| 文档 | 用途 |
|------|------|
| [docs/DOCKER部署.md](docs/DOCKER部署.md) | Docker Compose 部署与推送 GitHub 检查清单 |
| [docs/项目答辩逐字稿.md](docs/项目答辩逐字稿.md) | 答辩 / 面试口述稿 |
| [docs/面试讲点清单.md](docs/面试讲点清单.md) | 对着代码的面试口述稿 |
| [docs/分阶段提示词.md](docs/分阶段提示词.md) | 拆成 5 次对话重建项目 |
| [docs/深挖提示词.md](docs/深挖提示词.md) | Redis / Interrupt / RAG 等单点强化 |
