<template>
  <div class="tool fade-up">
    <div class="head">
      <span class="name mono">{{ tool.name }}</span>
      <span class="status" :class="tool.status">{{ statusLabel }}</span>
    </div>
    <details v-if="tool.input !== undefined">
      <summary>参数</summary>
      <pre class="mono">{{ format(tool.input) }}</pre>
    </details>
    <details v-if="tool.output" :open="resultOpen">
      <summary>结果{{ collapsedHint }}</summary>
      <pre class="mono">{{ tool.output }}</pre>
    </details>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ToolCallView } from '../stores/chat'

const props = defineProps<{
  tool: ToolCallView
  /** 父气泡仍在流式时默认折叠结果，避免淹没最终回答 */
  streaming?: boolean
}>()

const statusLabel = computed(() => {
  if (props.tool.status === 'running') return 'running'
  if (props.tool.status === 'error') return 'error'
  return 'done'
})

const resultOpen = computed(() => {
  if (props.tool.status === 'error') return true
  if (props.tool.status === 'running') return false
  if (props.streaming) return false
  // 超长 JSON 默认折叠，点开再看
  const out = props.tool.output || ''
  return out.length < 600
})

const collapsedHint = computed(() => {
  if (resultOpen.value) return ''
  const n = (props.tool.output || '').length
  return n > 600 ? `（已折叠 ${n} 字）` : '（点击展开）'
})

function format(v: unknown) {
  try {
    return JSON.stringify(v, null, 2)
  } catch {
    return String(v)
  }
}
</script>

<style scoped>
.tool {
  margin-top: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--bg-1);
}

.head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
  align-items: center;
}

.name {
  color: var(--teal);
  font-weight: 500;
}

.status {
  font-size: 0.7rem;
  letter-spacing: 0.04em;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 2px 7px;
  text-transform: uppercase;
}

.status.running {
  color: var(--warn);
  border-color: rgba(224, 168, 74, 0.4);
  animation: pulse 1s infinite;
}

.status.done {
  color: var(--success);
  border-color: rgba(35, 134, 91, 0.35);
}

.status.error {
  color: var(--danger);
  border-color: rgba(232, 106, 94, 0.4);
}

details {
  margin-top: 6px;
}

summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 0.82rem;
}

pre {
  margin: 6px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
  color: var(--ink);
  font-size: 0.76rem;
  line-height: 1.45;
  padding: 8px;
  border-radius: 6px;
  background: var(--bg-panel);
  border: 1px solid var(--line);
}
</style>
