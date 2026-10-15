<template>
  <section class="chat">
    <div class="toolbar surface">
      <div class="left">
        <label class="repo-field">
          <span>目标仓库</span>
          <input
            class="field mono"
            :value="store.repo"
            @change="onRepo"
            placeholder="owner/repo"
          />
        </label>
        <div class="meta-line">
          <StatusBadge :status="store.taskStatus" />
          <span v-if="store.taskRun?.current_step" class="step-now">
            {{ store.taskRun.current_step }}
          </span>
          <span v-else-if="currentStep" class="step-now">{{ currentStep }}</span>
          <span v-if="store.threadId" class="thread mono" :title="store.threadId">
            thread {{ shortThread }}
          </span>
        </div>
      </div>
      <div class="actions">
        <RouterLink v-if="store.planConfirm" class="btn btn-primary" to="/plan">
          确认计划
        </RouterLink>
        <RouterLink v-if="store.approval" class="btn btn-primary" to="/review">
          审查发布
        </RouterLink>
        <button
          v-if="canCancel"
          class="btn btn-danger"
          type="button"
          :disabled="store.taskBusy"
          @click="store.cancelCurrentTask()"
        >
          <AppIcon name="stop" :size="14" />
          取消任务
        </button>
        <button
          v-if="canRetry"
          class="btn btn-primary"
          type="button"
          :disabled="store.taskBusy || store.streaming"
          @click="store.retryCurrentTask()"
        >
          失败重试
        </button>
        <button class="btn" type="button" @click="store.newThread()" :disabled="store.streaming">
          新任务
        </button>
        <RouterLink class="btn btn-ghost" to="/">返回列表</RouterLink>
      </div>
    </div>

    <div class="workspace">
      <div class="main-pane">
        <div v-if="userPrompt" class="prompt-card surface">
          <div class="prompt-label">原始需求</div>
          <p>{{ userPrompt }}</p>
        </div>
        <StreamRenderer :messages="store.messages" @prompt="usePrompt" />
      </div>

      <aside class="side-pane surface">
        <h3>执行可视化</h3>
        <TaskExecutionPanel
          :task="store.taskRun"
          :busy="store.taskBusy"
          :live-elapsed-ms="store.liveElapsedMs"
          :envelopes="store.agentEnvelopes"
          @cancel="store.cancelCurrentTask()"
          @retry="store.retryCurrentTask()"
        />
        <div v-if="latestResult" class="side-block">
          <div class="side-label">PR 结果</div>
          <PullRequestSummary :result="latestResult" :repo="store.repo" />
        </div>
        <div v-if="latestTimeline.length" class="side-block">
          <div class="side-label">图节点时间线</div>
          <AgentTimeline :nodes="latestTimeline" />
        </div>
        <div v-if="toolSummary.length" class="side-block">
          <div class="side-label">工具调用</div>
          <ul class="tool-list">
            <li v-for="t in toolSummary" :key="t.id">
              <StatusBadge
                :status="t.status === 'done' ? 'done' : t.status === 'error' ? 'failed' : 'running'"
              />
              <span class="mono">{{ t.name }}</span>
            </li>
          </ul>
        </div>
      </aside>
    </div>

    <form class="composer surface" @submit.prevent="onSend">
      <textarea
        ref="ta"
        v-model="draft"
        class="field"
        rows="2"
        placeholder="继续下达指令，Enter 发送 · Shift+Enter 换行"
        :disabled="store.streaming"
        @keydown="onKey"
      />
      <button
        v-if="store.streaming"
        class="btn btn-danger send"
        type="button"
        @click="store.cancelCurrentTask()"
      >
        停止
      </button>
      <button v-else class="btn btn-primary send" type="submit" :disabled="!draft.trim()">
        发送
      </button>
    </form>
    <p v-if="store.error" class="err">{{ store.error }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import AgentTimeline from '../components/AgentTimeline.vue'
import AppIcon from '../components/ui/AppIcon.vue'
import PullRequestSummary from '../components/PullRequestSummary.vue'
import StatusBadge from '../components/ui/StatusBadge.vue'
import StreamRenderer from '../components/StreamRenderer.vue'
import TaskExecutionPanel from '../components/TaskExecutionPanel.vue'
import { useChatStore } from '../stores/chat'

const store = useChatStore()
const draft = ref('')
const ta = ref<HTMLTextAreaElement | null>(null)

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled'])
const SOFT_TERMINAL = new Set(['awaiting_coding', 'diff_approved'])

const canCancel = computed(
  () =>
    !!store.taskRun &&
    !TERMINAL.has(store.taskRun.status) &&
    !SOFT_TERMINAL.has(store.taskRun.status) &&
    !store.taskRun.cancel_requested,
)

const canRetry = computed(
  () =>
    !!store.taskRun &&
    (store.taskRun.status === 'failed' || store.taskRun.status === 'cancelled') &&
    !store.taskRun.tool_evidence?.publish_ok &&
    !store.taskRun.external?.pr_url,
)

const currentStep = computed(() => {
  if (!store.streaming) return ''
  for (let i = store.messages.length - 1; i >= 0; i--) {
    const m = store.messages[i]
    if (m.role !== 'agent') continue
    const tl = m.timeline || []
    const cur = [...tl].reverse().find((n) => n.status === 'enter') || tl[tl.length - 1]
    if (cur?.label || cur?.name) return String(cur.label || cur.name)
  }
  return ''
})

const userPrompt = computed(() => {
  return store.taskRun?.message || store.messages.find((m) => m.role === 'user')?.content || ''
})

const latestTimeline = computed(() => {
  for (let i = store.messages.length - 1; i >= 0; i--) {
    const m = store.messages[i]
    if (m.role === 'agent' && m.timeline?.length) return m.timeline
  }
  return []
})

const latestResult = computed(() => {
  for (let i = store.messages.length - 1; i >= 0; i--) {
    const m = store.messages[i]
    if (m.result) return m.result
  }
  return null
})

const toolSummary = computed(() => {
  for (let i = store.messages.length - 1; i >= 0; i--) {
    const m = store.messages[i]
    if (m.role === 'agent' && m.tools?.length) return m.tools.slice(-8)
  }
  return []
})

const shortThread = computed(() => {
  const t = store.threadId
  return t.length > 10 ? `${t.slice(0, 8)}…` : t
})

onMounted(async () => {
  const prefill = sessionStorage.getItem('ga_prefill')
  if (prefill) {
    sessionStorage.removeItem('ga_prefill')
    draft.value = prefill
    ta.value?.focus()
  }
  // 刷新后从后端恢复任务状态 / 日志
  await store.restoreTask()
})

function onRepo(e: Event) {
  store.setRepo((e.target as HTMLInputElement).value.trim())
}

function usePrompt(text: string) {
  draft.value = text
  ta.value?.focus()
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    onSend()
  }
}

async function onSend() {
  const text = draft.value
  if (!text.trim() || store.streaming) return
  draft.value = ''
  await store.send(text)
}
</script>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: flex-end;
  justify-content: space-between;
  flex-shrink: 0;
  padding: 12px 14px;
}

.left {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: min(420px, 100%);
}

.repo-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 500;
}

.meta-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.step-now {
  color: var(--accent);
  font-size: 0.8rem;
  font-weight: 500;
}

.thread {
  color: var(--muted-2);
  font-size: 0.72rem;
}

.actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.workspace {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  gap: 12px;
  overflow: hidden;
}

.main-pane {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: hidden;
}

.prompt-card {
  padding: 12px 14px;
  flex-shrink: 0;
}

.prompt-label {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--muted);
  margin-bottom: 4px;
}

.prompt-card p {
  margin: 0;
  font-size: 0.9rem;
  line-height: 1.5;
}

.side-pane {
  min-height: 0;
  overflow: auto;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.side-pane h3 {
  margin: 0;
  font-size: 0.88rem;
  font-weight: 650;
}

.side-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.side-label {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tool-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tool-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.78rem;
}

.composer {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  align-items: end;
  flex-shrink: 0;
  padding: 12px;
}

.composer .field {
  min-height: 72px;
  resize: vertical;
  max-height: 180px;
}

.send {
  min-width: 96px;
  height: 40px;
  align-self: end;
}

@media (max-width: 960px) {
  .workspace {
    grid-template-columns: 1fr;
  }

  .side-pane {
    max-height: 360px;
  }
}

@media (max-width: 640px) {
  .composer {
    grid-template-columns: 1fr;
  }

  .send {
    width: 100%;
  }
}
</style>
