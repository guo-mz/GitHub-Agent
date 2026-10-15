<template>
  <div class="bubble fade-up" :class="message.role">
    <div class="meta">
      <span class="who">{{ label }}</span>
      <span v-if="message.streaming" class="live">streaming</span>
    </div>
    <AgentTimeline v-if="message.timeline?.length" :nodes="message.timeline" />
    <p v-if="message.intent?.task_type" class="intent-bar mono">
      意图 {{ intentZh }} · {{ intentPct }}% · {{ message.intent.source || 'rules' }}
      <span v-if="intentSlots"> · {{ intentSlots }}</span>
    </p>
    <div v-if="message.role === 'agent'" class="body md" v-html="htmlBody" />
    <div v-else class="body" v-text="message.content || (message.streaming ? '…' : '')" />
    <ToolCallCard
      v-for="t in message.tools || []"
      :key="t.id"
      :tool="t"
      :streaming="!!message.streaming"
    />
    <PullRequestSummary
      v-if="message.result"
      :result="message.result"
      :repo="chat.repo"
    />
    <div
      v-if="message.role === 'agent' && !message.streaming && message.content?.trim()"
      class="save-actions"
    >
      <button class="btn tiny" type="button" :disabled="saving" @click="onDownload">
        下载 Markdown
      </button>
      <button class="btn tiny btn-primary" type="button" :disabled="saving" @click="onSaveRepo">
        {{ saving ? '保存中…' : '保存到仓库' }}
      </button>
      <label class="check" title="勾选后会直提 PR，绕过对话 HITL">
        <input v-model="alsoPr" type="checkbox" />
        同时提 PR（跳过 HITL）
      </label>
      <a v-if="prUrl" class="pr-link" :href="prUrl" target="_blank" rel="noreferrer">打开 PR</a>
      <span v-if="saveHint" class="save-hint" :class="{ err: saveErr }">{{ saveHint }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ChatMessage } from '../stores/chat'
import { useChatStore } from '../stores/chat'
import AgentTimeline from './AgentTimeline.vue'
import PullRequestSummary from './PullRequestSummary.vue'
import ToolCallCard from './ToolCallCard.vue'
import { renderMarkdown } from '../utils/markdown'

const props = defineProps<{ message: ChatMessage }>()
const chat = useChatStore()
const saving = ref(false)
const saveHint = ref('')
const saveErr = ref(false)
const prUrl = ref('')
const alsoPr = ref(false)

const label = computed(() => {
  if (props.message.role === 'user') return 'You'
  if (props.message.role === 'system') return 'System'
  return 'Agent'
})

const htmlBody = computed(() => {
  const text =
    props.message.content || (props.message.streaming ? '正在思考，请稍候…' : '')
  return renderMarkdown(text)
})

const intentZh = computed(() => {
  const m: Record<string, string> = {
    qa: '问答',
    read: '只读',
    review: '审查',
    write: '写入',
  }
  return m[props.message.intent?.task_type || ''] || props.message.intent?.task_type
})

const intentPct = computed(() =>
  Math.round((props.message.intent?.confidence || 0) * 100),
)

const intentSlots = computed(() => {
  const s = props.message.intent?.slots || {}
  const bits: string[] = []
  if (s.path) bits.push(String(s.path))
  if (s.pr_number) bits.push(`#${s.pr_number}`)
  if (s.branch) bits.push(String(s.branch))
  return bits.join(' ')
})

function onDownload() {
  chat.downloadAnswer(props.message.id)
  saveHint.value = '已下载'
  saveErr.value = false
}

async function onSaveRepo() {
  saving.value = true
  saveHint.value = ''
  saveErr.value = false
  prUrl.value = ''
  try {
    const data = await chat.saveAnswerToRepo(props.message.id, alsoPr.value)
    prUrl.value = data.pr_url || ''
    saveHint.value =
      data.detail ||
      (alsoPr.value
        ? `已保存并尝试提 PR → ${data.path || '仓库'}`
        : `已保存到分支 ${data.path || '仓库'}（未提 PR，符合 HITL）`)
  } catch (e: any) {
    saveErr.value = true
    const d = e?.response?.data?.detail
    saveHint.value = typeof d === 'string' ? d : e?.message || String(e)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.bubble {
  max-width: min(820px, 100%);
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: var(--bg-panel);
  margin-bottom: 10px;
  box-shadow: var(--shadow-sm);
}

.bubble.user {
  margin-left: auto;
  background: var(--accent-dim);
  border-color: rgba(98, 88, 232, 0.2);
}

.bubble.system {
  border-style: dashed;
  opacity: 0.92;
}

.meta {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
  color: var(--muted);
  font-size: 0.75rem;
  font-weight: 500;
  letter-spacing: 0.02em;
}

.who {
  text-transform: uppercase;
}

.live {
  color: var(--accent);
  animation: pulse 1.2s infinite;
}

.intent-bar {
  margin: 0 0 8px;
  font-size: 0.72rem;
  color: var(--muted);
  line-height: 1.4;
}

.body {
  white-space: pre-wrap;
  line-height: 1.6;
  word-break: break-word;
  font-size: 0.95rem;
}

.body.md {
  white-space: normal;
}

.body.md :deep(h2),
.body.md :deep(h3) {
  margin: 0.85em 0 0.4em;
  font-size: 1.02rem;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--ink);
}

.body.md :deep(h2:first-child),
.body.md :deep(h3:first-child),
.body.md :deep(p:first-child) {
  margin-top: 0;
}

.body.md :deep(p) {
  margin: 0.35em 0;
}

.body.md :deep(ul) {
  margin: 0.4em 0 0.6em;
  padding-left: 1.25em;
}

.body.md :deep(li) {
  margin: 0.25em 0;
}

.body.md :deep(strong) {
  color: var(--ink);
  font-weight: 650;
}

.body.md :deep(code) {
  font-family: var(--mono);
  font-size: 0.85em;
  background: var(--bg-1);
  padding: 0.1em 0.35em;
  border-radius: 4px;
  border: 1px solid var(--line);
}

.body.md :deep(.gap) {
  height: 0.5em;
}

.bubble :deep(.pr-summary) {
  margin-top: 12px;
  box-shadow: none;
}

.save-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--line);
}

.btn.tiny {
  font-size: 0.75rem;
  padding: 5px 10px;
  min-height: 0;
}

.check {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 0.72rem;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}

.pr-link {
  font-size: 0.78rem;
  color: var(--accent);
}

.save-hint {
  font-size: 0.75rem;
  color: var(--muted);
}

.save-hint.err {
  color: var(--danger);
}
</style>
