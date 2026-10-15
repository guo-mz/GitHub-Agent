-- 启用 pgvector，为代码 RAG 建表
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS code_chunks (
    id BIGSERIAL PRIMARY KEY,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    chunk_index INT NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (repo, path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_repo ON code_chunks (repo);
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding
    ON code_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- LLM 调用监控（每次真实模型尝试一行）
CREATE TABLE IF NOT EXISTS llm_call_records (
    id TEXT PRIMARY KEY,
    call_group_id TEXT,
    attempt_index INT DEFAULT 0,
    trace_id TEXT,
    task_id TEXT,
    conversation_id TEXT,
    module_name TEXT,
    node_name TEXT,
    provider TEXT,
    requested_model TEXT,
    actual_model TEXT,
    call_type TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    latency_ms INT,
    time_to_first_token_ms INT,
    input_tokens INT,
    output_tokens INT,
    cached_input_tokens INT,
    reasoning_tokens INT,
    total_tokens INT,
    estimated_cost DOUBLE PRECISION,
    currency TEXT,
    native_cost DOUBLE PRECISION,
    native_currency TEXT,
    fx_rate DOUBLE PRECISION,
    fx_as_of TEXT,
    fx_source TEXT,
    price_version TEXT,
    pricing_snapshot JSONB,
    estimate_limitations TEXT,
    status TEXT,
    error_type TEXT,
    retry_count INT DEFAULT 0,
    usage_source TEXT,
    storage_backend TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_created ON llm_call_records (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_task ON llm_call_records (task_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_module ON llm_call_records (module_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_call_records (actual_model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_call_records (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_trace ON llm_call_records (trace_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_group ON llm_call_records (call_group_id);
