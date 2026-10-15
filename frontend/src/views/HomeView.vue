<template>
  <section class="home page-scroll">
    <div class="hero fade-up">
      <p class="eyebrow">GitHub Agent</p>
      <h1>准备构建什么？</h1>
      <p class="lead">
        用自然语言让 AI 分析、修改 GitHub 项目；高危写操作会进入人工审批，确认后再创建 Pull Request。
      </p>
    </div>

    <TaskComposer
      v-model="draft"
      :repo="repo"
      :disabled="submitting"
      @update:repo="onRepo"
      @submit="onStart"
    />

    <p v-if="feedback" class="ok">{{ feedback }}</p>
    <p v-if="error" class="err">{{ error }}</p>

    <div class="recent-block">
      <div class="section-head">
        <h2>最近任务</h2>
        <RouterLink class="link" to="/approvals">查看全部</RouterLink>
      </div>

      <LoadingState v-if="loading" label="加载最近任务…" />
      <EmptyState
        v-else-if="!recent.length"
        icon="tasks"
        title="还没有任务记录"
        description="在上方选择仓库并描述需求，开始第一次 AI 研发任务。"
      >
        <button class="btn btn-primary" type="button" @click="focusComposer">新建任务</button>
      </EmptyState>
      <div v-else class="table-wrap surface">
        <table class="table">
          <thead>
            <tr>
              <th>任务</th>
              <th>仓库</th>
              <th>时间</th>
              <th>状态</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in recent" :key="item.id">
              <td>
                <div class="task-name">{{ rowTitle(item) }}</div>
              </td>
              <td class="mono muted">{{ item.repo || '—' }}</td>
              <td class="muted">{{ formatTs(rowTime(item)) }}</td>
              <td>
                <StatusBadge :status="rowStatus(item)" />
              </td>
              <td class="actions">
                <a
                  v-if="rowPr(item)"
                  :href="rowPr(item)!"
                  target="_blank"
                  rel="noreferrer"
                  class="btn btn-ghost"
                >
                  PR
                </a>
                <button class="btn btn-ghost" type="button" @click="openTask(item)">详情</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { fetchAuditHistory, fetchTaskRuns, type AuditEvent, type TaskRunView } from '../api'
import TaskComposer from '../components/TaskComposer.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import LoadingState from '../components/ui/LoadingState.vue'
import StatusBadge from '../components/ui/StatusBadge.vue'
import { useChatStore } from '../stores/chat'
import { rememberRepo } from '../utils/recent'

const store = useChatStore()
const router = useRouter()

const draft = ref('')
const repo = ref(store.repo)
const submitting = ref(false)
const loading = ref(false)
const recent = ref<(TaskRunView | AuditEvent)[]>([])
const error = ref('')
const feedback = ref('')
const usingTaskRuns = ref(false)

function onRepo(r: string) {
  repo.value = r
  store.setRepo(r)
  rememberRepo(r)
}

function formatTs(ts?: string) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

function isTaskRun(item: TaskRunView | AuditEvent): item is TaskRunView {
  return usingTaskRuns.value || 'current_step' in item || 'stages' in item
}

function rowTitle(item: TaskRunView | AuditEvent) {
  if (isTaskRun(item)) return item.message || item.current_step || item.id
  return item.summary || item.operation || item.kind || item.id
}

function rowTime(item: TaskRunView | AuditEvent) {
  if (isTaskRun(item)) return item.started_at || item.updated_at
  return item.ts
}

function rowStatus(item: TaskRunView | AuditEvent) {
  return item.status || (isTaskRun(item) ? 'ready' : (item as AuditEvent).kind) || 'ready'
}

function rowPr(item: TaskRunView | AuditEvent) {
  if (isTaskRun(item)) return item.external?.pr_url || undefined
  return item.pr_url
}

async function openTask(item: TaskRunView | AuditEvent) {
  if (isTaskRun(item) && item.thread_id) {
    localStorage.setItem('ga_thread', item.thread_id)
    store.threadId = item.thread_id
    await router.push('/chat')
    await store.restoreTask(item.id)
    return
  }
  await router.push('/chat')
}

function focusComposer() {
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

async function onStart({ text, repo: r }: { text: string; repo: string }) {
  if (!text.trim() || !r.includes('/') || submitting.value) return
  submitting.value = true
  error.value = ''
  feedback.value = ''
  try {
    store.setRepo(r)
    rememberRepo(r)
    store.newThread()
    feedback.value = '任务已创建，正在进入执行页…'
    await router.push('/chat')
    await store.send(text)
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  loading.value = true
  try {
    const tasks = await fetchTaskRuns({ limit: 12 })
    if (tasks.items?.length) {
      usingTaskRuns.value = true
      recent.value = tasks.items.slice(0, 8)
    } else {
      const data = await fetchAuditHistory({ limit: 20 })
      recent.value = (data.items || []).slice(0, 8)
    }
  } catch {
    try {
      const data = await fetchAuditHistory({ limit: 20 })
      recent.value = (data.items || []).slice(0, 8)
    } catch {
      recent.value = []
    }
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.home {
  max-width: 880px;
  margin: 0 auto;
  padding: 8px 4px 32px;
}

.hero {
  margin-bottom: 20px;
}

.eyebrow {
  margin: 0 0 8px;
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

h1 {
  margin: 0 0 10px;
  font-size: clamp(1.75rem, 3vw, 2.25rem);
  letter-spacing: -0.035em;
  font-weight: 700;
  line-height: 1.15;
}

.lead {
  margin: 0;
  color: var(--muted);
  font-size: 0.95rem;
  line-height: 1.6;
  max-width: 54ch;
}

.recent-block {
  margin-top: 28px;
}

.section-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 12px;
}

.section-head h2 {
  margin: 0;
  font-size: 1rem;
  font-weight: 650;
}

.link {
  font-size: 0.82rem;
}

.table-wrap {
  overflow: auto;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}

.table th {
  text-align: left;
  padding: 10px 14px;
  color: var(--muted);
  font-weight: 600;
  font-size: 0.75rem;
  border-bottom: 1px solid var(--line);
  background: var(--bg-1);
}

.table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  vertical-align: middle;
}

.table tr:last-child td {
  border-bottom: none;
}

.table tr:hover td {
  background: var(--bg-1);
}

.task-name {
  font-weight: 500;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.muted {
  color: var(--muted);
}

.actions {
  text-align: right;
  white-space: nowrap;
}

.actions .btn {
  min-height: 30px;
  padding: 4px 10px;
}
</style>
