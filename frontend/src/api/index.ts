import axios from 'axios'

// 直连后端，避免 Vite 代理缓冲 SSE 导致前端一直 “streaming…”
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

/**
 * 本地单操作员密钥（与 backend OPERATOR_API_TOKEN 一致）；非多用户 IAM。
 *
 * 安全限制（Phase-3）：
 * - 已废弃 VITE_OPERATOR_TOKEN：Vite 客户端变量会打进浏览器包，不能当秘密。
 * - 当前仍可用 localStorage.ga_operator_token（本机开发）；XSS 可窃取。
 * - 服务端未配置 OPERATOR_API_TOKEN 时 fail-closed，不会静默降级为无认证。
 * - 共享 Token 不适合公开互联网；完整方案应改为同源会话 + httpOnly Cookie + CSRF。
 */
export function getOperatorToken(): string {
  try {
    return (localStorage.getItem('ga_operator_token') || '').trim()
  } catch {
    return ''
  }
}

export function setOperatorToken(token: string) {
  try {
    localStorage.setItem('ga_operator_token', (token || '').trim())
  } catch {
    /* ignore quota / private mode */
  }
}

export function clearOperatorToken() {
  try {
    localStorage.removeItem('ga_operator_token')
  } catch {
    /* ignore */
  }
}

export function hasOperatorToken(): boolean {
  return Boolean(getOperatorToken())
}

function authHeaders(): Record<string, string> {
  const t = getOperatorToken()
  return t ? { 'X-Operator-Token': t } : {}
}

/** 用受保护接口校验操作员 Token 是否被服务端接受。 */
export async function verifyOperatorToken(token?: string): Promise<{
  ok: boolean
  status?: number
  detail?: string
}> {
  const t = (token ?? getOperatorToken()).trim()
  if (!t) return { ok: false, detail: '请输入操作员 Token' }
  try {
    const resp = await http.get('/api/approval/pending', {
      headers: { 'X-Operator-Token': t },
      timeout: 8000,
      validateStatus: () => true,
    })
    if (resp.status === 200) return { ok: true }
    const detail =
      typeof resp.data?.detail === 'string'
        ? resp.data.detail
        : resp.status === 401
          ? 'Token 无效或未认证'
          : resp.status === 503
            ? '服务暂时不可用'
            : `校验失败（HTTP ${resp.status}）`
    return { ok: false, status: resp.status, detail }
  } catch {
    return { ok: false, detail: '无法连接后端，请确认 API 已启动' }
  }
}

export const http = axios.create({
  baseURL: API_BASE,
  timeout: 60000,
})

http.interceptors.request.use((config) => {
  const h = authHeaders()
  if (h['X-Operator-Token']) {
    config.headers = config.headers || {}
    config.headers['X-Operator-Token'] = h['X-Operator-Token']
  }
  return config
})

http.interceptors.response.use(
  (resp) => resp,
  (err) => {
    const status = err?.response?.status
    if (status === 401) {
      clearOperatorToken()
      if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
        const redirect = encodeURIComponent(window.location.pathname + window.location.search)
        window.location.assign(`/login?redirect=${redirect}`)
      }
    }
    return Promise.reject(err)
  },
)

export type SSEHandler = (event: string, data: any) => void

/** 浏览器原生 fetch + ReadableStream 解析 SSE */
export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${API_BASE}${url}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
      ...authHeaders(),
    },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok || !resp.body) {
    const detail = await resp.text().catch(() => '')
    throw new Error(`SSE failed: ${resp.status}${detail ? ` ${detail.slice(0, 200)}` : ''}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let eventName = 'message'

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const chunks = buffer.split('\n\n')
    buffer = chunks.pop() || ''
    for (const chunk of chunks) {
      const lines = chunk.split('\n')
      let dataStr = ''
      for (const line of lines) {
        if (line.startsWith('event:')) {
          eventName = line.slice(6).trim()
        } else if (line.startsWith('data:')) {
          dataStr += (dataStr ? '\n' : '') + line.slice(5).trim()
        }
      }
      if (!dataStr) continue
      try {
        const data = JSON.parse(dataStr)
        onEvent(eventName || data.event || 'message', data)
      } catch {
        onEvent(eventName, { raw: dataStr })
      }
      eventName = 'message'
    }
  }
}

export async function fetchPendingApprovals() {
  const { data } = await http.get('/api/approval/pending')
  return data
}

export async function fetchAuditHistory(params?: { kind?: string; repo?: string; limit?: number }) {
  const { data } = await http.get('/api/approval/history', { params })
  return data as { ok: boolean; items: AuditEvent[] }
}

export async function fetchRepoCommitHistory(owner: string, repo: string, sha = 'main') {
  const { data } = await http.get(`/api/approval/commits/${owner}/${repo}`, {
    params: { sha, per_page: 20 },
  })
  return data as {
    ok: boolean
    repo: string
    sha: string
    remote: RemoteCommit[]
    local: AuditEvent[]
    error?: string
  }
}

export interface AuditEvent {
  id: string
  ts: string
  kind: string
  status?: string
  repo?: string
  operation?: string
  risk_level?: string
  summary?: string
  diff?: string
  path?: string
  branch?: string
  base_branch?: string
  commit_sha?: string
  pr_url?: string
  pr_number?: number
  message?: string
  thread_id?: string
  decision?: string
  comment?: string
  decided_at?: string
  rollback?: {
    branch_url?: string
    compare_url?: string
    commits_url?: string
    commit_url?: string
    pr_url?: string
    hint?: string
  }
}

export interface RemoteCommit {
  sha: string
  short_sha: string
  message: string
  author: string
  date: string
  html_url: string
  rollback?: { commit_url?: string; compare_url?: string; hint?: string }
}

export async function submitApproval(
  tid: string,
  decision: 'approve' | 'reject',
  comment: string,
  onEvent: SSEHandler,
  signal?: AbortSignal,
  opts?: { approval_kind?: string; task_id?: string },
) {
  return streamSSE(
    `/api/approval/${tid}`,
    {
      decision,
      comment,
      approval_kind: opts?.approval_kind,
      task_id: opts?.task_id || tid,
    },
    onEvent,
    signal,
  )
}

export async function fetchHealth() {
  const { data } = await http.get('/health', { timeout: 5000 })
  return data as {
    status: string
    redis: boolean
    postgres: boolean
    rag?: boolean
    rag_mode?: string
    checkpointer?: string
    rerank?: string
    version?: string
    auth_mode?: string
    mode?: string
  }
}

/** API 根地址（登录页展示 / GitHub OAuth 跳转用） */
export function getApiBase(): string {
  return API_BASE
}

export async function buildRepoGraph(owner: string, repo: string, ref = 'main', limit = 60) {
  const { data } = await http.post(
    `/api/repos/${owner}/${repo}/graph/index`,
    null,
    { params: { ref, limit }, timeout: 180000 },
  )
  return data as {
    ok?: boolean
    scanned?: number
    indexed?: number
    errors?: number
    stats?: { nodes?: number; edges?: number; files?: number }
    detail?: string
  }
}

export async function fetchRepoGraphViz(owner: string, repo: string, maxNodes = 60) {
  const { data } = await http.get(`/api/repos/${owner}/${repo}/graph/viz`, {
    params: { max_nodes: maxNodes },
    timeout: 15000,
  })
  return data as {
    ok?: boolean
    nodes?: { id: string; label: string; type?: string; path?: string }[]
    edges?: { source: string; target: string; rel?: string }[]
    stats?: { nodes?: number; edges?: number; files?: number }
    truncated?: boolean
  }
}

export async function indexRepoFile(owner: string, repo: string, path: string, ref = 'main') {
  const { data } = await http.post(`/api/repos/${owner}/${repo}/index`, null, {
    params: { path, ref },
    timeout: 120000,
  })
  return data
}

export async function fetchRepoTree(owner: string, repo: string, branch = 'main') {
  const { data } = await http.get(`/api/repos/${owner}/${repo}/tree`, { params: { branch } })
  return data
}

export async function fetchRepoFile(owner: string, repo: string, path: string, ref = 'main') {
  const { data } = await http.get(`/api/repos/${owner}/${repo}/file`, { params: { path, ref } })
  return data
}

export async function saveRepoFile(
  owner: string,
  repo: string,
  body: {
    path: string
    content: string
    message?: string
    base_branch?: string
    target_branch?: string
    sha?: string
    open_pr?: boolean
    pr_title?: string
    pr_body?: string
  },
) {
  const { data } = await http.put(`/api/repos/${owner}/${repo}/file`, body, { timeout: 120000 })
  return data
}

export async function aiEditFile(
  owner: string,
  repo: string,
  body: { path: string; content: string; instruction: string },
) {
  const { data } = await http.post(`/api/repos/${owner}/${repo}/ai-edit`, body, { timeout: 120000 })
  return data
}

/** AI 改代码 SSE：start / delta / done / error */
export async function streamAiEdit(
  owner: string,
  repo: string,
  body: { path: string; content: string; instruction: string },
  onEvent: SSEHandler,
  signal?: AbortSignal,
) {
  return streamSSE(`/api/repos/${owner}/${repo}/ai-edit/stream`, body, onEvent, signal)
}

export async function fetchGithubPermissions(repo: string) {
  const { data } = await http.get('/api/github/permissions', { params: { repo } })
  return data
}

export async function applyGithubToken(token: string, repo: string) {
  const { data } = await http.post('/api/github/token', { token, repo })
  return data
}

export async function fetchDeployKey(repo: string) {
  const { data } = await http.get('/api/github/deploy-key', { params: { repo } })
  return data
}

export async function verifyDeployKey(repo: string) {
  const { data } = await http.post('/api/github/deploy-key/verify', null, { params: { repo } })
  return data
}

export async function runEval(body: { include_live?: boolean; suites?: string[] } = {}) {
  const { data } = await http.post('/api/eval/run', body, { timeout: 120000 })
  return data as {
    ok?: boolean
    today?: string
    generated_at?: string
    note?: string
    metrics?: Record<string, number | null>
    results?: any[]
  }
}

/** 后端持久化的任务执行快照 */
export interface TaskRunView {
  id: string
  thread_id: string
  repo: string
  message: string
  status: string
  current_stage?: string | null
  current_step?: string
  stages: {
    id: string
    label: string
    status: string
    started_at?: string | null
    ended_at?: string | null
  }[]
  logs: { ts: string; level: string; message: string; seq: number }[]
  error?: string | null
  started_at?: string
  ended_at?: string | null
  elapsed_ms?: number | null
  event_seq?: number
  tool_evidence?: Record<string, boolean>
  external?: {
    pr_url?: string | null
    pr_number?: number | null
    commit_sha?: string | null
    branch?: string | null
  }
  phase?: string | null
  agent_envelopes?: Record<string, unknown>[]
  test_report?: Record<string, unknown>
  workspace_diff?: Record<string, unknown>
  diff_approved?: boolean
  publish_approved?: boolean
  cancel_requested?: boolean
  cancel_note?: string | null
  retry_of?: string | null
  updated_at?: string
}

export async function fetchTaskRuns(params?: { repo?: string; limit?: number }) {
  const { data } = await http.get('/api/chat/tasks', { params })
  return data as { ok: boolean; items: TaskRunView[] }
}

export async function fetchTaskRun(taskId: string) {
  const { data } = await http.get(`/api/chat/tasks/${taskId}`)
  return data as { ok: boolean; task: TaskRunView }
}

export async function cancelTaskRun(taskId: string) {
  const { data } = await http.post(`/api/chat/tasks/${taskId}/cancel`)
  return data as { ok: boolean; task: TaskRunView; note?: string }
}

export async function retryTaskRun(taskId: string) {
  const { data } = await http.post(`/api/chat/tasks/${taskId}/retry`, {})
  return data as {
    ok: boolean
    task: TaskRunView
    message?: string
    repo?: string
    note?: string
  }
}

export type LlmUsageFilters = {
  since?: string
  until?: string
  provider?: string
  model?: string
  module_name?: string
  task_id?: string
  status?: string
  offset?: number
  limit?: number
}

export async function fetchLlmUsageSummary(params?: LlmUsageFilters) {
  const { data } = await http.get('/api/llm-usage/summary', { params })
  return data
}

export async function fetchLlmUsageByModel(params?: LlmUsageFilters) {
  const { data } = await http.get('/api/llm-usage/by-model', { params })
  return data
}

export async function fetchLlmUsageByModule(params?: LlmUsageFilters) {
  const { data } = await http.get('/api/llm-usage/by-module', { params })
  return data
}

export async function fetchLlmUsageTrends(params?: LlmUsageFilters) {
  const { data } = await http.get('/api/llm-usage/trends/daily', { params })
  return data
}

export async function fetchLlmUsageCalls(params?: LlmUsageFilters) {
  const { data } = await http.get('/api/llm-usage/calls', { params })
  return data as {
    ok: boolean
    storage_backend?: string
    total: number
    offset: number
    limit: number
    items: Record<string, unknown>[]
  }
}

export async function fetchLlmUsageTask(taskId: string) {
  const { data } = await http.get(`/api/llm-usage/tasks/${taskId}`)
  return data
}
