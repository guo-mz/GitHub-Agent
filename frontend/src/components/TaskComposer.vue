<template>
  <form class="composer surface" @submit.prevent="submit">
    <div class="repo-row">
      <label class="label">
        GitHub 仓库
        <div class="repo-input">
          <AppIcon name="repo" :size="16" />
          <input
            v-model="repoLocal"
            class="field mono"
            placeholder="owner/repo"
            :disabled="disabled"
            list="recent-repos"
            @change="onRepoChange"
          />
          <datalist id="recent-repos">
            <option v-for="r in recent" :key="r" :value="r" />
          </datalist>
        </div>
      </label>
    </div>

    <label class="label grow">
      任务描述
      <textarea
        ref="ta"
        v-model="draft"
        class="field area"
        rows="5"
        placeholder="用自然语言描述你想对仓库做什么，例如：修复登录页的 TypeScript 类型错误并补充测试…"
        :disabled="disabled"
        @keydown="onKey"
      />
    </label>

    <div class="examples" v-if="showExamples">
      <span class="ex-label">示例任务</span>
      <div class="chips">
        <button
          v-for="ex in examples"
          :key="ex"
          type="button"
          class="chip"
          :disabled="disabled"
          @click="draft = ex"
        >
          {{ ex }}
        </button>
      </div>
    </div>

    <div class="footer">
      <p class="hint">Enter 发送 · Shift+Enter 换行</p>
      <button
        class="btn btn-primary start"
        type="submit"
        :disabled="disabled || !canSubmit"
      >
        <span v-if="disabled" class="spinner" />
        {{ disabled ? '提交中…' : '开始任务' }}
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from './ui/AppIcon.vue'

const props = withDefaults(
  defineProps<{
    modelValue?: string
    repo?: string
    disabled?: boolean
    showExamples?: boolean
  }>(),
  { modelValue: '', repo: '', disabled: false, showExamples: true },
)

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  (e: 'update:repo', v: string): void
  (e: 'submit', payload: { text: string; repo: string }): void
}>()

const draft = ref(props.modelValue)
const repoLocal = ref(props.repo)
const ta = ref<HTMLTextAreaElement | null>(null)
const recent = ref<string[]>([])

const examples = [
  '修复项目中的 Bug',
  '为指定模块补充单元测试',
  '增加一个新的功能',
  '修复 TypeScript 类型错误',
]

const canSubmit = computed(() => !!draft.value.trim() && !!repoLocal.value.trim() && repoLocal.value.includes('/'))

watch(
  () => props.modelValue,
  (v) => {
    if (v !== draft.value) draft.value = v
  },
)
watch(
  () => props.repo,
  (v) => {
    if (v !== repoLocal.value) repoLocal.value = v
  },
)
watch(draft, (v) => emit('update:modelValue', v))

function onRepoChange() {
  emit('update:repo', repoLocal.value.trim())
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    submit()
  }
}

function submit() {
  if (!canSubmit.value || props.disabled) return
  emit('update:repo', repoLocal.value.trim())
  emit('submit', { text: draft.value.trim(), repo: repoLocal.value.trim() })
}

onMounted(() => {
  try {
    const raw = localStorage.getItem('ga_recent_repos')
    recent.value = raw ? JSON.parse(raw) : []
  } catch {
    recent.value = []
  }
  ta.value?.focus()
})

defineExpose({ focus: () => ta.value?.focus() })
</script>

<style scoped>
.composer {
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--muted);
}

.repo-input {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
}

.repo-input .field {
  flex: 1;
}

.area {
  min-height: 120px;
  resize: vertical;
  font-size: 0.95rem;
  line-height: 1.55;
}

.examples {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ex-label {
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--muted-2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.chip {
  border: 1px solid var(--line);
  background: var(--bg-1);
  color: var(--ink);
  border-radius: 999px;
  padding: 6px 12px;
  font-size: 0.8rem;
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition);
}

.chip:hover:not(:disabled) {
  background: var(--accent-dim);
  border-color: rgba(98, 88, 232, 0.25);
  color: var(--accent);
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.hint {
  margin: 0;
  font-size: 0.75rem;
  color: var(--muted-2);
}

.start {
  min-width: 120px;
}

.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.4);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}

@media (max-width: 640px) {
  .footer {
    flex-direction: column;
    align-items: stretch;
  }

  .start {
    width: 100%;
  }
}
</style>
