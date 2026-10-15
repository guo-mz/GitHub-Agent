"""Pydantic Settings：所有敏感配置走环境变量，禁止硬编码（面试合规考点）。"""

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM 主链路（默认阿里云百炼 OpenAI 兼容）
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="LLM_BASE_URL",
    )
    llm_model: str = Field(default="qwen-plus", alias="LLM_MODEL")
    # 单次请求超时；超时后切下一层（DeepSeek → Ollama）
    llm_request_timeout: float = Field(default=25.0, alias="LLM_REQUEST_TIMEOUT")
    embedding_model: str = Field(default="text-embedding-3-small", alias="EMBEDDING_MODEL")

    # LLM 第二层：DeepSeek
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL"
    )
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")

    # LLM 兜底：Ollama（本地 OpenAI 兼容）
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434/v1", alias="OLLAMA_BASE_URL"
    )
    ollama_model: str = Field(default="qwen2.5:7b", alias="OLLAMA_MODEL")
    ollama_api_key: str = Field(default="ollama", alias="OLLAMA_API_KEY")

    # GitHub
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_client_id: str = Field(default="", alias="GITHUB_CLIENT_ID")
    github_client_secret: str = Field(default="", alias="GITHUB_CLIENT_SECRET")
    github_oauth_redirect_uri: str = Field(
        default="http://localhost:8000/api/auth/callback",
        alias="GITHUB_OAUTH_REDIRECT_URI",
    )
    github_webhook_secret: str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")
    # 仅本地调试：未配置 WEBHOOK SECRET 时允许跳过签名（生产默认拒绝）
    allow_insecure_webhook: bool = Field(default=False, alias="ALLOW_INSECURE_WEBHOOK")

    # Infra（微服可分库：Agent 用 /1，Repo/注册表用 /0 + Postgres）
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    registry_redis_url: str = Field(default="", alias="REGISTRY_REDIS_URL")
    database_url: str = Field(
        default="postgresql+asyncpg://agent:agent_secret@localhost:5432/github_agent",
        alias="DATABASE_URL",
    )

    # Langfuse
    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="http://localhost:3001", alias="LANGFUSE_HOST")

    # App
    app_secret_key: str = Field(default="dev-secret-change-me", alias="APP_SECRET_KEY")
    # 本地单操作员共享密钥（非多用户身份）。空且未开 insecure → 敏感写默认拒绝。
    operator_api_token: str = Field(default="", alias="OPERATOR_API_TOKEN")
    allow_insecure_no_auth: bool = Field(default=False, alias="ALLOW_INSECURE_NO_AUTH")
    # MemorySaver / 无 Redis 时是否允许启动依赖 interrupt 恢复的写任务（默认否）
    allow_ephemeral_hitl: bool = Field(default=False, alias="ALLOW_EPHEMERAL_HITL")
    # 本地演示：无可信测试执行器时仍允许走发布（默认否；生产禁止）
    allow_untrusted_publish: bool = Field(default=False, alias="ALLOW_UNTRUSTED_PUBLISH")
    # 本地演示：无 Redis 时用进程内幂等完成发布（默认否；不能跨进程防重）
    allow_publish_without_redis: bool = Field(
        default=False, alias="ALLOW_PUBLISH_WITHOUT_REDIS"
    )
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="CORS_ORIGINS",
    )
    default_repo: str = Field(default="", alias="DEFAULT_REPO")
    rate_limit_github_per_hour: int = Field(default=4500, alias="RATE_LIMIT_GITHUB_PER_HOUR")
    agent_recursion_limit: int = Field(default=25, alias="AGENT_RECURSION_LIMIT")

    # Agent 重试兜底（LLM / 工具瞬时故障）
    agent_llm_max_retries: int = Field(default=3, alias="AGENT_LLM_MAX_RETRIES")
    agent_tool_max_retries: int = Field(default=2, alias="AGENT_TOOL_MAX_RETRIES")
    agent_stream_max_retries: int = Field(default=1, alias="AGENT_STREAM_MAX_RETRIES")
    agent_retry_base_delay: float = Field(default=0.6, alias="AGENT_RETRY_BASE_DELAY")

    # 微服务（Gateway 反代；单体模式可忽略）
    gateway_mode: str = Field(default="monolith", alias="GATEWAY_MODE")  # monolith | micro
    agent_svc_url: str = Field(default="http://127.0.0.1:8011", alias="AGENT_SVC_URL")
    repo_svc_url: str = Field(default="http://127.0.0.1:8012", alias="REPO_SVC_URL")
    service_name: str = Field(default="", alias="SERVICE_NAME")  # agent | repo | gateway
    service_advertise_url: str = Field(default="", alias="SERVICE_ADVERTISE_URL")
    repo_rpc_enabled: bool = Field(default=False, alias="REPO_RPC_ENABLED")
    repo_rpc_url: str = Field(default="http://127.0.0.1:8012", alias="REPO_RPC_URL")
    # 空默认：未显式配置时内部 RPC fail-closed（禁止 dev-internal-rpc 等弱默认）
    internal_rpc_token: str = Field(default="", alias="INTERNAL_RPC_TOKEN")
    breaker_failure_threshold: int = Field(default=5, alias="BREAKER_FAILURE_THRESHOLD")
    breaker_recovery_sec: float = Field(default=20.0, alias="BREAKER_RECOVERY_SEC")

    # Jev（TypeSafe System One 决策层）
    jev_enabled: bool = Field(default=True, alias="JEV_ENABLED")
    typesafe_api_key: str = Field(default="", alias="TYPESAFE_API_KEY")
    jev_model: str = Field(default="jev-latest", alias="JEV_MODEL")
    jev_confidence_threshold: float = Field(default=0.7, alias="JEV_CONFIDENCE_THRESHOLD")
    jev_escalate_threshold: float = Field(default=0.7, alias="JEV_ESCALATE_THRESHOLD")

    # Transformer Cross-Encoder 精排（Hybrid RAG；未装模型则关键词回退）
    transformer_rerank_enabled: bool = Field(default=True, alias="TRANSFORMER_RERANK_ENABLED")
    transformer_rerank_model: str = Field(
        default="cross-encoder/ms-marco-MiniLM-L-6-v2",
        alias="TRANSFORMER_RERANK_MODEL",
    )

    # 上下文工程（进 LLM 前裁剪 / 摘要；checkpointer 仍保留完整轨迹）
    context_max_tokens: int = Field(default=6000, alias="CONTEXT_MAX_TOKENS")
    context_keep_recent: int = Field(default=12, alias="CONTEXT_KEEP_RECENT")
    context_tool_max_chars: int = Field(default=4000, alias="CONTEXT_TOOL_MAX_CHARS")
    context_llm_summary: bool = Field(default=True, alias="CONTEXT_LLM_SUMMARY")

    # Guardrails
    max_files_changed: int = 5
    max_lines_changed: int = 200

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
