<template>
  <section class="approvals page-scroll">
    <div class="head">
      <div>
        <h1 class="page-title">Pull Requests / 任务历史</h1>
        <p class="page-hint">
          待审批即时处理；审批记录与提交历史本地持久化，并附 GitHub 回滚与 PR 链接。
        </p>
      </div>
      <button class="btn" @click="refresh" :disabled="loading">
        刷新
      </button>
    </div>

    <div class="tabs">
      <button
        v-for="t in tabs"
        :key="t.id"
        type="button"
        :class="['tab', { active: tab === t.id }]"
        @click="switchTab(t.id)"
      >
        {{ t.label }}
        <span v-if="t.id === 'pending' && items.length" class="badge">{{ items.length }}</span>
      </button>
    </div>

    <div class="filters" v-if="tab !== 'commits'">
      <input
        v-model="query"
        class="field"
        type="search"
        placeholder="搜索操作、仓库或摘要…"
      />
    </div>

    <!-- 待审批 -->
    <template v-if="tab === 'pending'">
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!filteredPending.length" class="empty">
        {{ items.length ? '没有匹配的待审批项' : '当前没有待审批项。对话里触发 create_pr / merge 时会出现在这里。' }}
      </div>
      <article v-for="item in filteredPending" :key="item.thread_id" class="item fade-up">
        <header>
          <strong>{{ item.operation }}</strong>
          <span class="risk" :data-level="item.risk_level">{{ item.risk_level }}</span>
          <span v-if="item.approval_kind" class="kind mono">{{ item.approval_kind }}</span>
        </header>
        <p class="meta mono">{{ item.repo }} · thread {{ item.thread_id }}</p>
        <p class="summary">{{ item.summary }}</p>
        <pre v-if="item.approval_kind !== 'plan_confirm'" class="diff mono">{{ item.diff || '（无 diff）' }}</pre>
        <textarea
          v-model="comments[item.thread_id]"
          class="field remark"
          rows="2"
          placeholder="审批备注（可选）"
          :disabled="busy === item.thread_id"
        />
        <footer>
          <span v-if="busy === item.thread_id" class="busy-hint">处理中…</span>
          <button
            v-if="item.approval_kind === 'plan_confirm'"
            class="btn btn-primary"
            type="button"
            :disabled="busy === item.thread_id"
            @click="openPlan(item)"
          >
            打开计划确认
          </button>
          <button
            v-else
            class="btn"
            type="button"
            :disabled="busy === item.thread_id"
            @click="openReview(item)"
          >
            审查 Diff
          </button>
          <button class="btn btn-danger" :disabled="busy === item.thread_id" @click="decide(item, 'reject')">
            拒绝
          </button>
          <button class="btn btn-success" :disabled="busy === item.thread_id" @click="decide(item, 'approve')">
            {{ item.approval_kind === 'plan_confirm' ? '确认计划' : '批准发布' }}
          </button>
        </footer>
      </article>
    </template>

    <!-- 审批记录 -->
    <template v-else-if="tab === 'approvals'">
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!filteredApprovals.length" class="empty">
        {{ approvalHistory.length ? '没有匹配的记录' : '尚无审批记录。批准/拒绝后会写到本地审计日志。' }}
      </div>
      <article v-for="ev in filteredApprovals" :key="ev.id" class="item fade-up">
        <header>
          <strong>{{ ev.operation || 'approval' }}</strong>
          <span class="status" :data-status="ev.status">{{ statusLabel(ev.status) }}</span>
        </header>
        <p class="meta mono">
          {{ formatTs(ev.ts) }}
          <span v-if="ev.decided_at"> · 决定于 {{ formatTs(ev.decided_at) }}</span>
          <span v-if="ev.repo"> · {{ ev.repo }}</span>
          <span v-if="ev.thread_id"> · {{ ev.thread_id }}</span>
        </p>
        <p class="summary">{{ ev.summary || '—' }}</p>
        <pre v-if="ev.diff" class="diff mono">{{ ev.diff }}</pre>
        <p v-if="ev.comment" class="comment">备注：{{ ev.comment }}</p>
        <div class="links" v-if="ev.rollback">
          <a v-if="ev.rollback.pr_url || ev.pr_url" :href="ev.rollback.pr_url || ev.pr_url" target="_blank" rel="noreferrer">
            打开 PR
          </a>
          <a v-if="ev.rollback.compare_url" :href="ev.rollback.compare_url" target="_blank" rel="noreferrer">
            Compare / 回滚对比
          </a>
          <a v-if="ev.rollback.branch_url" :href="ev.rollback.branch_url" target="_blank" rel="noreferrer">
            分支
          </a>
          <span class="hint">{{ ev.rollback.hint }}</span>
        </div>
      </article>
    </template>

    <!-- 提交历史 -->
    <template v-else>
      <div class="repo-bar">
        <input v-model="ownerRepo" class="field mono" placeholder="owner/repo" />
        <input v-model="branch" class="field mono branch" placeholder="branch" />
        <button class="btn" type="button" :disabled="loading" @click="loadCommits">拉取</button>
      </div>
      <div v-if="commitError" class="warn">{{ commitError }}</div>
      <div v-if="loading" class="empty">加载中…</div>

      <h2 class="sub">本 Agent 提交（编辑器保存）</h2>
      <div v-if="!loading && !localCommits.length" class="empty sm">暂无本地提交记录。</div>
      <article v-for="ev in localCommits" :key="ev.id" class="item fade-up">
        <header>
          <strong class="mono">{{ ev.path || ev.branch }}</strong>
          <span class="status" data-status="success">saved</span>
        </header>
        <p class="meta mono">
          {{ formatTs(ev.ts) }} · {{ ev.branch }}
          <span v-if="ev.commit_sha"> · {{ String(ev.commit_sha).slice(0, 7) }}</span>
        </p>
        <p class="summary">{{ ev.summary || ev.message || '—' }}</p>
        <div class="links" v-if="ev.rollback">
          <a v-if="ev.pr_url || ev.rollback.pr_url" :href="ev.pr_url || ev.rollback.pr_url" target="_blank" rel="noreferrer">
            PR
          </a>
          <a v-if="ev.rollback.compare_url" :href="ev.rollback.compare_url" target="_blank" rel="noreferrer">
            Compare
          </a>
          <a v-if="ev.rollback.commit_url" :href="ev.rollback.commit_url" target="_blank" rel="noreferrer">
            Commit
          </a>
          <a v-if="ev.rollback.branch_url" :href="ev.rollback.branch_url" target="_blank" rel="noreferrer">
            分支
          </a>
          <span class="hint">{{ ev.rollback.hint }}</span>
        </div>
      </article>

      <h2 class="sub">GitHub 远程提交</h2>
      <div v-if="!loading && !remoteCommits.length" class="empty sm">暂无远程提交（检查仓库名 / Token）。</div>
      <article v-for="c in remoteCommits" :key="c.sha" class="item fade-up">
        <header>
          <strong class="mono">{{ c.short_sha }}</strong>
          <span class="meta">{{ c.author }}</span>
        </header>
        <p class="summary">{{ c.message }}</p>
        <p class="meta mono">{{ formatTs(c.date) }}</p>
        <div class="links">
          <a v-if="c.html_url" :href="c.html_url" target="_blank" rel="noreferrer">打开 Commit</a>
          <a v-if="c.rollback?.compare_url" :href="c.rollback.compare_url" target="_blank" rel="noreferrer">
            Diff
          </a>
          <span v-if="c.rollback?.hint" class="hint">{{ c.rollback.hint }}</span>
        </div>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  type AuditEvent,
  type RemoteCommit,
  fetchAuditHistory,
  fetchPendingApprovals,
  fetchRepoCommitHistory,
  submitApproval,
} from '../api'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const router = useRouter()

const tabs = [
  { id: 'pending' as const, label: '待审批' },
  { id: 'approvals' as const, label: '审批记录' },
  { id: 'commits' as const, label: '提交历史' },
]
type TabId = (typeof tabs)[number]['id']

const tab = ref<TabId>('pending')
type PendingItem = {
  thread_id: string
  operation: string
  risk_level: string
  repo: string
  summary: string
  diff: string
  approval_kind?: string
  payload?: Record<string, unknown>
  task_id?: string
}

const items = ref<PendingItem[]>([])
const history = ref<AuditEvent[]>([])
const remoteCommits = ref<RemoteCommit[]>([])
const loading = ref(false)
const busy = ref('')
const comments = ref<Record<string, string>>({})
const commitError = ref('')
const ownerRepo = ref(chat.repo || 'guo-mz/-')
const branch = ref('main')
const query = ref('')

const approvalHistory = computed(() => history.value.filter((e) => e.kind === 'approval'))
const localCommits = computed(() => {
  const repo = ownerRepo.value.trim()
  const fromHistory = history.value.filter((e) => e.kind === 'commit' && (!repo || e.repo === repo))
  return fromHistory
})

const filteredPending = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter(
    (i) =>
      i.operation?.toLowerCase().includes(q) ||
      i.repo?.toLowerCase().includes(q) ||
      i.summary?.toLowerCase().includes(q),
  )
})

const filteredApprovals = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return approvalHistory.value
  return approvalHistory.value.filter(
    (e) =>
      e.operation?.toLowerCase().includes(q) ||
      e.repo?.toLowerCase().includes(q) ||
      e.summary?.toLowerCase().includes(q) ||
      e.status?.toLowerCase().includes(q),
  )
})

function statusLabel(s?: string) {
  if (s === 'approved') return '已批准'
  if (s === 'rejected') return '已拒绝'
  if (s === 'pending') return '待处理'
  if (s === 'success') return '成功'
  return s || '—'
}

function formatTs(ts?: string) {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ts
  }
}

async function loadPending() {
  items.value = await fetchPendingApprovals()
}

async function loadHistory() {
  const data = await fetchAuditHistory({ limit: 100 })
  history.value = data.items || []
}

async function loadCommits() {
  commitError.value = ''
  const [owner, repo] = ownerRepo.value.trim().split('/')
  if (!owner || !repo) {
    commitError.value = '请填写 owner/repo'
    return
  }
  chat.setRepo(`${owner}/${repo}`)
  loading.value = true
  try {
    await loadHistory()
    const data = await fetchRepoCommitHistory(owner, repo, branch.value.trim() || 'main')
    remoteCommits.value = data.remote || []
    if (data.local?.length) {
      // 合并远端接口里的 local，避免漏
      const ids = new Set(history.value.map((h) => h.id))
      for (const ev of data.local) {
        if (!ids.has(ev.id)) history.value.push(ev)
      }
    }
    if (data.error) commitError.value = data.error
  } catch (e: any) {
    commitError.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

async function refresh() {
  loading.value = true
  try {
    if (tab.value === 'pending') await loadPending()
    else if (tab.value === 'approvals') await loadHistory()
    else await loadCommits()
  } finally {
    loading.value = false
  }
}

async function switchTab(id: TabId) {
  tab.value = id
  await refresh()
}

async function decide(item: PendingItem, decision: 'approve' | 'reject') {
  busy.value = item.thread_id
  const comment = (comments.value[item.thread_id] || '').trim()
  const kind = item.approval_kind || 'publish'
  try {
    await submitApproval(item.thread_id, decision, comment, () => {}, undefined, {
      approval_kind: kind,
      task_id: item.task_id || item.thread_id,
    })
    items.value = items.value.filter((i) => i.thread_id !== item.thread_id)
    delete comments.value[item.thread_id]
  } finally {
    busy.value = ''
    await loadPending()
    await loadHistory()
  }
}

function openPlan(item: PendingItem) {
  chat.planConfirm = {
    thread_id: item.thread_id,
    payload: item.payload || {
      repo: item.repo,
      summary: item.summary,
      analysis_report: (item.payload as any)?.analysis_report,
      plan_document: (item.payload as any)?.plan_document,
    },
    approval_kind: 'plan_confirm',
  }
  router.push('/plan')
}

function openReview(item: PendingItem) {
  chat.approval = {
    thread_id: item.thread_id,
    payload: {
      repo: item.repo,
      summary: item.summary,
      diff: item.diff,
      tool_name: item.operation,
      risk_level: item.risk_level,
      approval_kind: item.approval_kind || 'publish',
    },
    risk_level: item.risk_level,
    diff: item.diff,
    operation: item.operation,
    approval_kind: item.approval_kind || 'publish',
  }
  chat.showApprovalModal = false
  router.push('/review')
}

onMounted(refresh)
</script>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 8px;
}

.tabs {
  display: flex;
  gap: 6px;
  margin: 12px 0 12px;
  border-bottom: 1px solid var(--line);
  padding-bottom: 8px;
}

.filters {
  margin-bottom: 12px;
}

.tab {
  appearance: none;
  background: transparent;
  border: 1px solid transparent;
  color: var(--muted);
  padding: 8px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 0.88rem;
  font-weight: 500;
}

.tab.active {
  color: var(--accent);
  background: var(--accent-dim);
  border-color: rgba(98, 88, 232, 0.22);
}

.badge {
  display: inline-block;
  margin-left: 6px;
  min-width: 1.2em;
  padding: 0 5px;
  border-radius: 999px;
  font-size: 0.7rem;
  background: rgba(224, 168, 74, 0.25);
  color: var(--warn);
}

.repo-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.field {
  flex: 1;
  min-width: 160px;
}

.field.branch {
  flex: 0 0 120px;
  min-width: 100px;
}

.sub {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--muted);
  margin: 18px 0 8px;
  letter-spacing: 0.02em;
}

.item {
  margin-top: 12px;
  padding: 16px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--bg-panel);
}

header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.risk,
.status,
.kind {
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 4px 8px;
  border-radius: 6px;
  border: 1px solid rgba(224, 168, 74, 0.35);
  background: rgba(224, 168, 74, 0.12);
  color: var(--warn);
}

.kind {
  border-color: var(--line-strong);
  background: var(--bg-1);
  color: var(--muted);
  text-transform: none;
}

.risk[data-level='critical'],
.status[data-status='rejected'] {
  border-color: rgba(232, 106, 94, 0.4);
  background: rgba(232, 106, 94, 0.12);
  color: var(--danger);
}

.status[data-status='approved'],
.status[data-status='success'] {
  border-color: rgba(35, 134, 91, 0.35);
  background: var(--success-dim);
  color: var(--success);
}

.meta {
  color: var(--muted);
  font-size: 0.78rem;
  margin: 8px 0;
}

.summary {
  margin: 0 0 10px;
  line-height: 1.5;
}

.comment {
  margin: 0 0 8px;
  color: var(--muted);
  font-size: 0.85rem;
}

.diff {
  background: var(--bg-1);
  padding: 10px;
  border-radius: 8px;
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  font-size: 0.78rem;
  border: 1px solid var(--line);
  margin: 0 0 8px;
}

.links {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  font-size: 0.82rem;
}

.links a {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.hint {
  color: var(--muted);
  font-size: 0.75rem;
  flex: 1 1 100%;
  line-height: 1.4;
}

.remark {
  margin-top: 10px;
  width: 100%;
  resize: vertical;
}

.busy-hint {
  margin-right: auto;
  color: var(--warn);
  font-size: 0.8rem;
  align-self: center;
}

footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
  align-items: center;
}

.empty {
  color: var(--muted);
  padding: 28px;
  text-align: center;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
}

.empty.sm {
  padding: 16px;
  font-size: 0.85rem;
}

.warn {
  color: var(--warn);
  font-size: 0.82rem;
  margin-bottom: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(224, 168, 74, 0.3);
  background: rgba(224, 168, 74, 0.08);
}
</style>
