<template>
  <div class="viewer">
    <div class="toolbar">
      <div class="file-meta">
        <AppIcon name="file" :size="15" />
        <span class="mono path">{{ active?.path || '—' }}</span>
        <StatusBadge v-if="active" :status="active.status" />
      </div>
      <div class="stats mono" v-if="stats">
        <span class="add">+{{ stats.additions }}</span>
        <span class="del">-{{ stats.deletions }}</span>
        <span class="files">{{ stats.files }} files</span>
      </div>
    </div>
    <div ref="scroller" class="body mono">
      <template v-if="lines.length">
        <div
          v-for="(line, i) in visibleLines"
          :key="i"
          class="line"
          :data-kind="line.kind"
        >
          {{ line.text }}
        </div>
        <button
          v-if="lines.length > limit"
          type="button"
          class="btn btn-ghost more"
          @click="limit += 400"
        >
          加载更多（仍有 {{ lines.length - limit }} 行）
        </button>
      </template>
      <div v-else class="empty">选择左侧文件查看 Diff，或当前审批无 diff 摘要。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DiffFile, DiffStats } from '../../utils/diff'
import { toDiffLines } from '../../utils/diff'
import AppIcon from '../ui/AppIcon.vue'
import StatusBadge from '../ui/StatusBadge.vue'

const props = defineProps<{
  files: DiffFile[]
  selected?: string
  stats?: DiffStats
}>()

const limit = ref(500)
const scroller = ref<HTMLElement | null>(null)

const active = computed(() => {
  if (!props.files.length) return null
  return props.files.find((f) => f.path === props.selected) || props.files[0]
})

const lines = computed(() => (active.value ? toDiffLines(active.value.raw) : []))
const visibleLines = computed(() => lines.value.slice(0, limit.value))

watch(
  () => props.selected,
  () => {
    limit.value = 500
    if (scroller.value) scroller.value.scrollTop = 0
  },
)
</script>

<style scoped>
.viewer {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  background: var(--bg-panel);
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}

.file-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--muted);
}

.path {
  font-size: 0.8rem;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stats {
  display: flex;
  gap: 10px;
  font-size: 0.75rem;
  color: var(--muted);
}

.add {
  color: var(--success);
}

.del {
  color: var(--danger);
}

.body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  font-size: 0.78rem;
  line-height: 1.5;
}

.line {
  padding: 0 14px;
  white-space: pre;
  border-left: 3px solid transparent;
}

.line[data-kind='add'] {
  background: var(--diff-add-bg);
  border-left-color: var(--success);
}

.line[data-kind='del'] {
  background: var(--diff-del-bg);
  border-left-color: var(--danger);
}

.line[data-kind='meta'] {
  color: var(--muted);
  background: var(--bg-1);
}

.empty {
  padding: 40px 20px;
  text-align: center;
  color: var(--muted);
}

.more {
  margin: 12px auto 20px;
  display: block;
}
</style>
