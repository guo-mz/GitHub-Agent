<template>
  <section class="usage page-scroll">
    <div class="head">
      <div>
        <h1 class="page-title">AI 用量监控</h1>
        <p class="page-hint">
          查看每次模型调用的实际模型、耗时、Token 与估算费用（非账单实付）。存储：{{
            storageBackend || '—'
          }}
        </p>
      </div>
      <button class="btn btn-primary" :disabled="loading" type="button" @click="reload">
        {{ loading ? '加载中…' : '刷新' }}
      </button>
    </div>

    <div class="filters">
      <label>
        起
        <input v-model="since" type="datetime-local" />
      </label>
      <label>
        止
        <input v-model="until" type="datetime-local" />
      </label>
      <label>
        Provider
        <input v-model="provider" placeholder="aliyun / deepseek" />
      </label>
      <label>
        模型
        <input v-model="model" placeholder="实际或请求模型" />
      </label>
      <label>
        模块
        <input v-model="moduleName" placeholder="coding / chat.agent" />
      </label>
      <label>
        任务 ID
        <input v-model="taskId" placeholder="task id" />
      </label>
      <label>
        状态
        <select v-model="status">
          <option value="">全部</option>
          <option value="ok">ok</option>
          <option value="error">error</option>
          <option value="timeout">timeout</option>
          <option value="cancelled">cancelled</option>
        </select>
      </label>
      <button class="btn" type="button" @click="reload">应用筛选</button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p class="note">{{ costNote }}</p>
    <p v-if="usageGapHint" class="note warn">{{ usageGapHint }}</p>

    <div class="cards">
      <div class="card">
        <div class="k">调用总次数</div>
        <div class="v mono">{{ fmtNum(summary?.call_count) }}</div>
      </div>
      <div class="card">
        <div class="k">输入 Token</div>
        <div class="v mono">{{ fmtToken(summary?.input_tokens, summary?.input_tokens_unknown) }}</div>
      </div>
      <div class="card">
        <div class="k">输出 Token</div>
        <div class="v mono">{{ fmtToken(summary?.output_tokens, summary?.output_tokens_unknown) }}</div>
      </div>
      <div class="card">
        <div class="k">累计 Token</div>
        <div class="v mono">{{ fmtToken(summary?.total_tokens, summary?.total_tokens_unknown) }}</div>
      </div>
      <div class="card accent">
        <div class="k">估算总费用 (CNY)</div>
        <div class="v mono">{{ fmtCost(summary?.estimated_cost, summary?.estimated_cost_unknown) }}</div>
      </div>
      <div class="card">
        <div class="k">平均耗时</div>
        <div class="v mono">{{ fmtMs(summary?.avg_latency_ms) }}</div>
      </div>
      <div class="card">
        <div class="k">失败率</div>
        <div class="v mono">{{ fmtPct(summary?.failure_rate) }}</div>
      </div>
    </div>

    <h2 class="sec">模型使用排行</h2>
    <table v-if="models.length" class="table">
      <thead>
        <tr>
          <th>模型</th>
          <th>Provider</th>
          <th>次数</th>
          <th>Token</th>
          <th>估算费用</th>
          <th>平均耗时</th>
          <th>失败率</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in models" :key="m.provider + m.model">
          <td class="mono">{{ m.model || '—' }}</td>
          <td class="mono">{{ m.provider || '—' }}</td>
          <td class="mono">{{ m.call_count }}</td>
          <td class="mono">{{ fmtToken(m.total_tokens, 0) }}</td>
          <td class="mono">{{ fmtCost(m.estimated_cost, m.estimated_cost_unknown) }}</td>
          <td class="mono">{{ fmtMs(m.avg_latency_ms) }}</td>
          <td class="mono">{{ fmtPct(m.failure_rate) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">暂无模型统计</div>

    <h2 class="sec">模块使用统计</h2>
    <table v-if="modules.length" class="table">
      <thead>
        <tr>
          <th>模块</th>
          <th>次数</th>
          <th>输入 Token</th>
          <th>输出 Token</th>
          <th>费用</th>
          <th>平均耗时</th>
          <th>P95</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="m in modules" :key="m.module_name">
          <td class="mono">{{ m.module_name }}</td>
          <td class="mono">{{ m.call_count }}</td>
          <td class="mono">{{ fmtToken(m.input_tokens, 0) }}</td>
          <td class="mono">{{ fmtToken(m.output_tokens, 0) }}</td>
          <td class="mono">{{ fmtCost(m.estimated_cost, m.estimated_cost_unknown) }}</td>
          <td class="mono">{{ fmtMs(m.avg_latency_ms) }}</td>
          <td class="mono">{{ fmtMs(m.p95_latency_ms) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">暂无模块统计</div>

    <h2 class="sec">每日趋势</h2>
    <div v-if="trends.length" class="trends">
      <div v-for="t in trends" :key="t.date" class="trend-row">
        <div class="trend-label mono">{{ t.date }}</div>
        <div class="bars">
          <div
            class="bar calls"
            :style="{ width: barWidth(t.call_count, maxCalls) }"
            :title="`调用 ${t.call_count}`"
          />
          <div
            class="bar tokens"
            :style="{ width: barWidth(t.total_tokens ?? 0, maxTokens) }"
            :title="`Token ${fmtToken(t.total_tokens, 0)}`"
          />
          <div
            class="bar fail"
            :style="{ width: barWidth(t.failure_count, maxFails || 1) }"
            :title="`失败 ${t.failure_count}`"
          />
        </div>
        <div class="trend-meta mono">
          n={{ t.call_count }} · tok={{ fmtToken(t.total_tokens, 0) }} · ￥{{
            fmtCost(t.estimated_cost, t.estimated_cost_unknown)
          }}
          · fail={{ t.failure_count }}
        </div>
      </div>
      <div class="legend">
        <span class="lg calls">调用次数</span>
        <span class="lg tokens">Token</span>
        <span class="lg fail">失败</span>
      </div>
    </div>
    <div v-else class="empty">暂无趋势数据</div>

    <h2 class="sec">调用明细</h2>
    <table v-if="calls.length" class="table compact">
      <thead>
        <tr>
          <th>时间</th>
          <th>任务</th>
          <th>模块/节点</th>
          <th>实际模型</th>
          <th>耗时</th>
          <th>入/出/总</th>
          <th>费用</th>
          <th>状态</th>
          <th>错误</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="c in calls" :key="String(c.id)">
          <td class="mono">{{ shortTime(c.created_at || c.started_at) }}</td>
          <td class="mono">
            <button
              v-if="c.task_id"
              type="button"
              class="link"
              @click="openTask(String(c.task_id))"
            >
              {{ shortId(String(c.task_id)) }}
            </button>
            <span v-else>—</span>
          </td>
          <td class="mono">{{ c.module_name }}/{{ c.node_name }}</td>
          <td class="mono">{{ c.actual_model || '未知' }}</td>
          <td class="mono">{{ fmtMs(c.latency_ms as number | null) }}</td>
          <td class="mono">
            {{ fmtToken(c.input_tokens as number | null, null) }}/{{
              fmtToken(c.output_tokens as number | null, null)
            }}/{{ fmtToken(c.total_tokens as number | null, null) }}
          </td>
          <td class="mono">{{ fmtCost(c.estimated_cost as number | null, null) }}</td>
          <td>
            <span :class="['tag', c.status === 'ok' ? 'ok' : 'bad']">{{ c.status || '—' }}</span>
          </td>
          <td class="mono muted">{{ c.error_type || '—' }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">暂无调用记录</div>
    <div v-if="callTotal > calls.length" class="pager">
      <button class="btn" type="button" :disabled="offset <= 0" @click="prevPage">上一页</button>
      <span class="mono">{{ offset + 1 }}–{{ offset + calls.length }} / {{ callTotal }}</span>
      <button
        class="btn"
        type="button"
        :disabled="offset + calls.length >= callTotal"
        @click="nextPage"
      >
        下一页
      </button>
    </div>

    <div v-if="taskDetail" class="task-panel">
      <div class="task-head">
        <h2 class="sec">任务下钻 · {{ taskDetail.task_id }}</h2>
        <button class="btn btn-ghost" type="button" @click="taskDetail = null">关闭</button>
      </div>
      <p class="note">
        墙钟 {{ fmtMs(taskDetail.task_wall_clock_ms) }} · 模型累计
        {{ fmtMs(taskDetail.model_latency_sum_ms) }} ·
        {{ taskDetail.latency_note }}
      </p>
      <p class="note">
        调用 {{ taskDetail.call_count }} · Token
        {{ fmtToken(taskDetail.total_tokens, taskDetail.total_tokens_unknown) }} · 估算
        {{ fmtCost(taskDetail.estimated_cost, taskDetail.estimated_cost_unknown) }}
      </p>
      <table v-if="taskDetail.calls?.length" class="table compact">
        <thead>
          <tr>
            <th>模块</th>
            <th>模型</th>
            <th>耗时</th>
            <th>Token</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in taskDetail.calls" :key="c.id">
            <td class="mono">{{ c.module_name }}/{{ c.node_name }}</td>
            <td class="mono">{{ c.actual_model || '未知' }}</td>
            <td class="mono">{{ fmtMs(c.latency_ms) }}</td>
            <td class="mono">{{ fmtToken(c.total_tokens, null) }}</td>
            <td class="mono">{{ c.status }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  fetchLlmUsageByModel,
  fetchLlmUsageByModule,
  fetchLlmUsageCalls,
  fetchLlmUsageSummary,
  fetchLlmUsageTask,
  fetchLlmUsageTrends,
} from '../api'

const loading = ref(false)
const error = ref('')
const storageBackend = ref('')
const summary = ref<Record<string, any> | null>(null)
const models = ref<any[]>([])
const modules = ref<any[]>([])
const trends = ref<any[]>([])
const calls = ref<Record<string, unknown>[]>([])
const callTotal = ref(0)
const offset = ref(0)
const limit = 40
const taskDetail = ref<Record<string, any> | null>(null)

const since = ref('')
const until = ref('')
const provider = ref('')
const model = ref('')
const moduleName = ref('')
const taskId = ref('')
const status = ref('')

const costNote = computed(
  () =>
    summary.value?.cost_note ||
    '估算费用（CNY），非服务商账单；未知 Token/价格显示为「未知」或「—」，不用 0 伪装。',
)

/** 有调用但 Token 全未知：多半是当时响应未带 usage，不是价格配错 */
const usageGapHint = computed(() => {
  const s = summary.value
  if (!s || !s.call_count) return ''
  const tokUnknown =
    s.total_tokens == null && (s.total_tokens_unknown ?? 0) >= (s.call_count ?? 0)
  if (!tokUnknown) return ''
  return (
    '当前筛选范围内的调用未拿到服务商 Token 用量（usage_source=unavailable），' +
    '因此 Token 与费用显示「未知」。耗时与模型名仍可用。请新跑一轮对话后再刷新；' +
    '旧记录不会自动补全 usage。'
  )
})

const maxCalls = computed(() => Math.max(1, ...trends.value.map((t) => t.call_count || 0)))
const maxTokens = computed(() =>
  Math.max(1, ...trends.value.map((t) => (typeof t.total_tokens === 'number' ? t.total_tokens : 0))),
)
const maxFails = computed(() => Math.max(0, ...trends.value.map((t) => t.failure_count || 0)))

function toIsoLocal(v: string): string | undefined {
  if (!v) return undefined
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return undefined
  return d.toISOString()
}

function filters(extra?: Record<string, unknown>) {
  return {
    since: toIsoLocal(since.value),
    until: toIsoLocal(until.value),
    provider: provider.value || undefined,
    model: model.value || undefined,
    module_name: moduleName.value || undefined,
    task_id: taskId.value || undefined,
    status: status.value || undefined,
    ...extra,
  }
}

function fmtNum(v: unknown) {
  if (v == null || typeof v !== 'number') return '—'
  return String(v)
}

function fmtToken(v: unknown, unknownCount: unknown) {
  // 没有任何调用时用 —，避免「0 次调用却显示未知 Token」造成误解
  if ((summary.value?.call_count ?? 0) === 0 && v == null) return '—'
  if (v == null) return '未知'
  if (typeof v !== 'number') return '—'
  if (typeof unknownCount === 'number' && unknownCount > 0) {
    return `${v}（另有 ${unknownCount} 次未知）`
  }
  return String(v)
}

function fmtCost(v: unknown, unknownCount: unknown) {
  if ((summary.value?.call_count ?? 0) === 0 && v == null) return '—'
  if (v == null) return '未知'
  if (typeof v !== 'number') return '—'
  const s = v < 0.01 && v > 0 ? v.toFixed(6) : v.toFixed(4)
  if (typeof unknownCount === 'number' && unknownCount > 0) {
    return `${s}（${unknownCount} 次未知）`
  }
  return s
}

function fmtMs(v: unknown) {
  if (v == null || typeof v !== 'number') return '—'
  return `${Math.round(v)} ms`
}

function fmtPct(v: unknown) {
  if (v == null || typeof v !== 'number') return '—'
  return `${(v * 100).toFixed(1)}%`
}

function barWidth(v: number, max: number) {
  const pct = Math.max(2, Math.round((v / Math.max(max, 1)) * 100))
  return `${pct}%`
}

function shortTime(v: unknown) {
  if (!v || typeof v !== 'string') return '—'
  return v.replace('T', ' ').slice(0, 19)
}

function shortId(id: string) {
  return id.length > 10 ? `${id.slice(0, 8)}…` : id
}

async function openTask(id: string) {
  try {
    taskDetail.value = await fetchLlmUsageTask(id)
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || String(e)
  }
}

async function reload() {
  loading.value = true
  error.value = ''
  try {
    const f = filters()
    const [s, bm, bmod, tr, cl] = await Promise.all([
      fetchLlmUsageSummary(f),
      fetchLlmUsageByModel(f),
      fetchLlmUsageByModule(f),
      fetchLlmUsageTrends(f),
      fetchLlmUsageCalls({ ...f, offset: offset.value, limit }),
    ])
    summary.value = s
    storageBackend.value = s.storage_backend || cl.storage_backend || ''
    models.value = bm.items || []
    modules.value = bmod.items || []
    trends.value = tr.items || []
    calls.value = cl.items || []
    callTotal.value = cl.total || 0
  } catch (e: any) {
    const status = e?.response?.status
    const detail = e?.response?.data?.detail || e?.message || String(e)
    if (status === 404) {
      error.value =
        `接口 404（${detail}）：后端未加载 /api/llm-usage。请重启 API（当前 VITE_API_BASE 指向的端口），再刷新本页。`
    } else if (status === 401) {
      error.value = `未认证（401）：请重新登录并填写有效 Operator Token。`
    } else {
      error.value = status ? `HTTP ${status}: ${detail}` : detail
    }
  } finally {
    loading.value = false
  }
}

function prevPage() {
  offset.value = Math.max(0, offset.value - limit)
  reload()
}

function nextPage() {
  offset.value = offset.value + limit
  reload()
}

onMounted(() => {
  reload()
})
</script>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
  align-items: flex-end;
}

.filters label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 0.75rem;
  color: var(--muted);
}

.filters input,
.filters select {
  min-width: 140px;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--bg-panel);
  color: inherit;
}

.cards {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 18px;
}

.card {
  flex: 1 1 calc(25% - 10px);
  min-width: 160px;
  box-sizing: border-box;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 12px;
  min-height: 84px;
}

.card.accent {
  border-color: rgba(98, 88, 232, 0.35);
  background: var(--accent-dim);
}

.k {
  color: var(--muted);
  font-size: 0.75rem;
  margin-bottom: 6px;
}

.v {
  font-size: 1.2rem;
  font-weight: 600;
}

.sec {
  font-size: 1rem;
  margin: 18px 0 8px;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84rem;
  margin-bottom: 8px;
}

.table.compact {
  font-size: 0.78rem;
}

.table th,
.table td {
  border-bottom: 1px solid var(--line);
  padding: 7px 8px;
  text-align: left;
  vertical-align: top;
}

.table th {
  color: var(--muted);
  font-weight: 500;
}

.note {
  color: var(--muted);
  font-size: 0.8rem;
  margin: 0 0 10px;
}

.note.warn {
  color: #a67c00;
  background: rgba(250, 204, 21, 0.12);
  border: 1px solid rgba(250, 204, 21, 0.35);
  border-radius: 6px;
  padding: 8px 10px;
}

.err {
  color: var(--danger);
  margin-bottom: 8px;
}

.empty {
  color: var(--muted);
  padding: 18px;
  text-align: center;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
  margin-bottom: 12px;
}

.tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.72rem;
  font-family: var(--mono, ui-monospace, monospace);
}

.tag.ok {
  color: var(--success);
  border: 1px solid rgba(35, 134, 91, 0.35);
  background: var(--success-dim);
}

.tag.bad {
  color: var(--danger);
  border: 1px solid rgba(240, 113, 120, 0.35);
}

.trends {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.trend-row {
  display: grid;
  grid-template-columns: 96px 1fr minmax(180px, 240px);
  gap: 10px;
  align-items: center;
}

.bars {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.bar {
  height: 6px;
  border-radius: 3px;
  min-width: 4px;
}

.bar.calls {
  background: #3b82f6;
}
.bar.tokens {
  background: #10b981;
}
.bar.fail {
  background: #f07178;
}

.legend {
  display: flex;
  gap: 12px;
  font-size: 0.72rem;
  color: var(--muted);
  margin-top: 4px;
}

.lg::before {
  content: '';
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: -1px;
}

.lg.calls::before {
  background: #3b82f6;
}
.lg.tokens::before {
  background: #10b981;
}
.lg.fail::before {
  background: #f07178;
}

.trend-meta {
  font-size: 0.72rem;
  color: var(--muted);
}

.link {
  background: none;
  border: none;
  color: var(--accent, #6280e8);
  cursor: pointer;
  padding: 0;
  font: inherit;
  text-decoration: underline;
}

.pager {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 8px 0 16px;
}

.task-panel {
  margin-top: 16px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
}

.task-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.muted {
  color: var(--muted);
}

@media (max-width: 900px) {
  .trend-row {
    grid-template-columns: 1fr;
  }
}
</style>
