<template>
  <div v-if="open" class="overlay" @click.self="$emit('close')">
    <div class="modal fade-up" role="dialog" aria-modal="true">
      <header>
        <p class="eyebrow">Human in the loop</p>
        <h2>高危操作审批</h2>
        <p class="sub">{{ operation || '敏感写操作' }} · 风险 {{ risk || 'high' }}</p>
        <p class="hint">批准后才会调用 GitHub 写接口；拒绝则无副作用。建议在完整审查页查看 Diff。</p>
      </header>
      <div class="diff mono">
        <pre>{{ diff || '（无 diff 摘要）' }}</pre>
      </div>
      <textarea v-model="comment" class="field" rows="2" placeholder="审批备注（可选）" />
      <footer>
        <RouterLink class="btn" to="/review" @click="$emit('close')">完整审查</RouterLink>
        <button class="btn" type="button" @click="$emit('close')">稍后</button>
        <button class="btn btn-danger" type="button" @click="reject">拒绝</button>
        <button class="btn btn-success" type="button" @click="approve">同意并继续</button>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

const props = defineProps<{
  open: boolean
  diff?: string
  operation?: string
  risk?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'approve', comment: string): void
  (e: 'reject', comment: string): void
}>()

const comment = ref('')
watch(
  () => props.open,
  (v) => {
    if (v) comment.value = ''
  },
)

function approve() {
  emit('approve', comment.value)
}
function reject() {
  emit('reject', comment.value)
}
</script>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(28, 25, 23, 0.45);
  backdrop-filter: blur(6px);
  display: grid;
  place-items: center;
  z-index: 50;
  padding: 20px;
}

.modal {
  width: min(680px, 100%);
  background: var(--bg-panel);
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 22px;
  box-shadow: var(--shadow-lg);
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--accent);
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

h2 {
  margin: 0 0 4px;
  font-size: 1.25rem;
  letter-spacing: -0.02em;
}

.sub {
  margin: 0 0 6px;
  color: var(--muted);
  font-size: 0.9rem;
}

.hint {
  margin: 0 0 14px;
  color: var(--muted);
  font-size: 0.78rem;
}

.diff {
  background: var(--bg-1);
  border-radius: 8px;
  padding: 12px;
  max-height: 280px;
  overflow: auto;
  margin-bottom: 12px;
  border: 1px solid var(--line);
}

pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.8rem;
  line-height: 1.45;
}

footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
  flex-wrap: wrap;
}
</style>
