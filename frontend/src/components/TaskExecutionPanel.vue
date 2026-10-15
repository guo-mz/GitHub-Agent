<template>
  <div class="panel">
    <div class="row">
      <StatusBadge :status="task?.status || 'ready'" />
      <span v-if="task?.current_step" class="step">{{ task.current_step }}</span>
    </div>

    <div v-if="task" class="meta mono">
      <span>已耗时 {{ elapsedLabel }}</span>
      <span v-if="task.started_at">· 开始 {{ formatTs(task.started_at) }}</span>
      <span v-if="task.ended_at">· 结束 {{ formatTs(task.ended_at) }}</span>
    </div>

    <ol v-if="task?.stages?.length" class="stages">
      <li
        v-for="s in task.stages"
        :key="s.id"
        :data-status="s.status"
        :data-current="s.id === task.current_stage"
      >
        <span class="dot" />
        <span class="label">{{ s.label }}</span>
        <span class="st">{{ stageLabel(s.status) }}</span>
      </li>
    </ol>
    <p v-else class="hint">阶段将在 Agent 实际进入对应节点后出现（不预填未适用步骤）。</p>

    <div v-if="agents.length" class="agents">
      <div class="side-label">Agent 状态</div>
      <ul>
        <li v-for="a in agents" :key="a.agent_name + String(a.started_at || '')" :data-status="a.status">
          <span class="name">{{ agentLabel(a.agent_name) }}</span>
          <span class="ast">{{ agentStatusLabel(a.status) }}</span>
        </li>
      </ul>
      <p v-if="hasNotRun" class="hint">not_run = 无可靠沙箱未执行测试，不代表测试通过。</p>
    </div>

    <div v-if="task?.error" class="err-box">
      <div class="side-label">失败原因</div>
      <p>{{ friendlyError }}</p>
      <p v-if="task.cancel_note" class="hint">{{ task.cancel_note }}</p>
      <p v-if="isToken403" class="hint">
        修复：给 Token 开通仓库 Contents 写 + Pull requests 写（经典 PAT 勾选 repo），更新
        <code>backend/.env</code> 的 <code>GITHUB_TOKEN</code>，重启后端后再点「失败重试」。
      </p>
    </div>

    <div class="logs" v-if="visibleLogs.length">
      <div class="side-label">执行日志</div>
      <div class="log-list mono">
        <div v-for="l in visibleLogs" :key="l.seq" :data-level="l.level">
          <span class="t">{{ formatTs(l.ts) }}</span>
          {{ l.message }}
        </div>
      </div>
    </div>

    <div class="ops">
      <button
        v-if="canCancel"
        class="btn btn-danger"
        type="button"
        :disabled="busy"
        @click="$emit('cancel')"
      >
        取消任务
      </button>
      <button
        v-if="canRetry"
        class="btn btn-primary"
        type="button"
        :disabled="busy"
        @click="$emit('retry')"
      >
        失败重试
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TaskRunView } from '../api'
import StatusBadge from './ui/StatusBadge.vue'

const props = defineProps<{
  task: TaskRunView | null
  busy?: boolean
  liveElapsedMs?: number | null
  envelopes?: Record<string, unknown>[]
}>()

defineEmits<{ (e: 'cancel'): void; (e: 'retry'): void }>()

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])
const SOFT_TERMINAL = new Set(['awaiting_coding', 'diff_approved'])

const canCancel = computed(
  () =>
    !!props.task &&
    !TERMINAL.has(props.task.status) &&
    !SOFT_TERMINAL.has(props.task.status) &&
    !props.task.cancel_requested,
)
const canRetry = computed(
  () =>
    !!props.task &&
    (props.task.status === 'failed' || props.task.status === 'cancelled') &&
    !props.task.tool_evidence?.publish_ok &&
    !props.task.external?.pr_url,
)

const elapsedLabel = computed(() => {
  const ms = props.liveElapsedMs ?? props.task?.elapsed_ms ?? 0
  const s = Math.floor(ms / 1000)
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}分${r}秒` : `${r}秒`
})

const visibleLogs = computed(() => (props.task?.logs || []).slice(-40).reverse())

type AgentRow = { agent_name: string; status: string; started_at?: string }

const agents = computed((): AgentRow[] => {
  const list = (props.envelopes || []) as AgentRow[]
  const seen = new Map<string, AgentRow>()
  for (const a of list) {
    if (!a?.agent_name) continue
    seen.set(a.agent_name, a)
  }
  return [...seen.values()]
})

const hasNotRun = computed(() => agents.value.some((a) => a.status === 'not_run'))

const friendlyError = computed(() => {
  const raw = String(props.task?.error || '')
  if (!raw) return ''
  try {
    const start = raw.indexOf('{')
    if (start >= 0) {
      const obj = JSON.parse(raw.slice(start))
      if (obj?.message) {
        if (String(obj.message).toLowerCase().includes('personal access token')) {
          return `GitHub Token 权限不足（${obj.status || 403}）：${obj.message}`
        }
        return String(obj.message)
      }
    }
  } catch {
    /* keep raw */
  }
  return raw
})

const isToken403 = computed(() => {
  const e = String(props.task?.error || '').toLowerCase()
  return e.includes('personal access token') || e.includes('github token 权限不足')
})

function stageLabel(st: string) {
  if (st === 'running') return '进行中'
  if (st === 'waiting') return '等待执行'
  if (st === 'done') return '完成'
  if (st === 'failed') return '失败'
  if (st === 'skipped') return '跳过'
  return '待执行'
}

function agentLabel(name: string) {
  const map: Record<string, string> = {
    analysis: 'Analysis',
    planning: 'Planning',
    coding: 'Coding',
    review: 'Review',
    testing: 'Testing',
    publish: 'Publish',
  }
  return map[name] || name
}

function agentStatusLabel(st: string) {
  if (st === 'succeeded' || st === 'done') return '完成'
  if (st === 'running') return '进行中'
  if (st === 'failed') return '失败'
  if (st === 'not_run') return 'not_run'
  return st || '—'
}

function formatTs(ts?: string | null) {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleTimeString()
  } catch {
    return ts
  }
}
</script>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.step {
  font-size: 0.82rem;
  color: var(--accent);
  font-weight: 500;
}

.meta {
  font-size: 0.72rem;
  color: var(--muted);
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.stages {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.stages li {
  display: grid;
  grid-template-columns: 12px 1fr auto;
  gap: 8px;
  align-items: center;
  font-size: 0.82rem;
  color: var(--muted);
}

.stages li[data-status='running'],
.stages li[data-current='true'] {
  color: var(--accent);
  font-weight: 600;
}

.stages li[data-status='done'] {
  color: var(--ink);
}

.stages li[data-status='failed'] {
  color: var(--danger);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--line-strong);
}

.stages li[data-status='running'] .dot,
.stages li[data-current='true'] .dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-dim);
}

.stages li[data-status='done'] .dot {
  background: var(--success);
}

.st {
  font-size: 0.68rem;
  color: var(--muted-2);
}

.hint {
  margin: 0;
  font-size: 0.78rem;
  color: var(--muted);
  line-height: 1.45;
}

.agents ul {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.agents li {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 0.8rem;
  padding: 4px 0;
  border-bottom: 1px solid var(--line);
}

.agents li[data-status='not_run'] .ast {
  color: var(--muted-2);
}

.agents li[data-status='succeeded'] .ast,
.agents li[data-status='done'] .ast {
  color: var(--success);
}

.agents li[data-status='failed'] .ast {
  color: var(--danger);
}

.agents .name {
  font-weight: 500;
}

.err-box {
  padding: 10px;
  border-radius: 8px;
  background: var(--danger-dim);
  border: 1px solid rgba(200, 77, 85, 0.25);
}

.err-box p {
  margin: 4px 0 0;
  font-size: 0.82rem;
  color: var(--danger);
}

.side-label {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.logs {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
}

.log-list {
  max-height: 180px;
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-1);
  padding: 8px;
  font-size: 0.7rem;
  line-height: 1.45;
}

.log-list div {
  margin-bottom: 4px;
  color: var(--ink);
}

.log-list div[data-level='error'] {
  color: var(--danger);
}

.log-list div[data-level='warn'] {
  color: var(--warn);
}

.t {
  color: var(--muted-2);
  margin-right: 6px;
}

.ops {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
