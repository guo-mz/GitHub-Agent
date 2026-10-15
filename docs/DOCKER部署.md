# Docker 一键部署说明

面向：克隆本仓库后在本机用 Docker Compose 拉起完整环境，并安全推送到 GitHub（密钥不入库）。

## 1. 前置条件

- 已安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)（含 `docker compose`）
- 至少准备：
  - `LLM_API_KEY`（OpenAI 兼容，如 DeepSeek / 通义）
  - `GITHUB_TOKEN`（PAT，建议含 `repo`）
  - `OPERATOR_API_TOKEN`（自定义长随机串，登录前端用）

## 2. 配置环境变量（必做）

```bash
cd github-agent
cp .env.example .env
```

用编辑器打开 `.env`，至少改这三项：

```env
LLM_API_KEY=你的密钥
GITHUB_TOKEN=你的_PAT
OPERATOR_API_TOKEN=自己设一个足够长的随机串
```

其余可用默认值。Compose 会在容器内自动把 `REDIS_URL` / `DATABASE_URL` 改成服务名 `redis` / `postgres`，无需把 `.env` 里的 localhost 改成容器名。

> **禁止**把 `.env`、`backend/.env`、`frontend/.env` 提交到 GitHub。仓库已用 `.gitignore` 忽略。

## 3. 启动（单体，推荐）

```bash
docker compose up -d --build
```

| 服务 | 地址 |
|------|------|
| 前端 | http://localhost:5173 |
| 后端 API / Swagger | http://localhost:8000/docs |
| Redis | localhost:6379 |
| Postgres + pgvector | localhost:5432 |

登录页填入与 `.env` 中相同的 `OPERATOR_API_TOKEN`。

查看状态 / 日志：

```bash
docker compose ps
docker compose logs -f backend
docker compose logs -f frontend
```

停止：

```bash
docker compose down
# 连数据卷一起删（慎用）：
# docker compose down -v
```

## 4. 可选：Langfuse 可观测

```bash
docker compose --profile obs up -d --build
```

Langfuse UI：http://localhost:3001  
注册后把 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 写回 `.env`，再重启 backend。

## 5. 可选：微服务模式

```bash
# .env 中设置（>=16 字符随机串，禁止弱默认）：
# INTERNAL_RPC_TOKEN=用 python -c "import secrets; print(secrets.token_urlsafe(32))" 生成

docker compose -f docker-compose.micro.yml up -d --build
```

| 服务 | 地址 |
|------|------|
| Gateway（前端应指向它） | http://localhost:8001 |
| Agent | http://localhost:8011 |
| Repo | http://localhost:8012 |
| 前端 | http://localhost:5173（`VITE_API_BASE=http://localhost:8001`） |

## 6. 推送到 GitHub 前检查清单

1. 确认仓库根目录是 `github-agent/`（或你 fork 后的项目根）。
2. 确认 `.env` **不会**出现在 `git status` 里。
3. 只提交 `.env.example`，他人克隆后自行复制填写。
4. 若密钥曾经误提交过，请到 GitHub / LLM 平台**轮换**密钥。

```bash
git init
git add .
git status   # 确认没有 .env
git commit -m "Initial commit: GitHub Agent with Docker Compose"
# 在 GitHub 新建空仓库后：
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

## 7. 常见问题

| 现象 | 处理 |
|------|------|
| backend 一直 unhealthy | `docker compose logs backend`；检查 `.env` 是否存在、依赖是否装全 |
| 前端能开但接口 401 | `OPERATOR_API_TOKEN` 未配或与浏览器 localStorage 不一致 |
| 容器内连不上 Redis | 确认用 `docker compose up` 启动；不要只用本机 `.env` 里的 `localhost` 覆盖 compose 的 `environment` |
| GitHub 写操作失败 | PAT 权限不足（Contents / Pull requests）；见 Publish 错误提示 |
| 端口占用 | 改 compose 的 `ports` 映射，或关掉占用 5173/8000 的进程 |

## 8. 架构（单体 Compose）

```
浏览器 → frontend:5173
       → backend:8000  → redis / postgres(pgvector)
       （可选 profile obs）→ langfuse:3001
```
