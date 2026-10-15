<template>
  <aside class="tree">
    <div class="head">
      <span>变更文件</span>
      <span class="count mono">{{ files.length }}</span>
    </div>
    <button
      v-for="f in files"
      :key="f.path"
      type="button"
      class="file"
      :class="{ active: f.path === selected }"
      @click="$emit('select', f.path)"
    >
      <span class="status" :data-status="f.status">{{ statusChar(f.status) }}</span>
      <span class="path mono" :title="f.path">{{ f.path }}</span>
      <span class="stats mono">
        <em class="add">+{{ f.additions }}</em>
        <em class="del">-{{ f.deletions }}</em>
      </span>
    </button>
    <div v-if="!files.length" class="empty">暂无文件变更</div>
  </aside>
</template>

<script setup lang="ts">
import type { DiffFile } from '../../utils/diff'

defineProps<{
  files: DiffFile[]
  selected?: string
}>()
defineEmits<{ (e: 'select', path: string): void }>()

function statusChar(s: DiffFile['status']) {
  if (s === 'added') return 'A'
  if (s === 'deleted') return 'D'
  if (s === 'renamed') return 'R'
  return 'M'
}
</script>

<style scoped>
.tree {
  border-right: 1px solid var(--line);
  background: var(--bg-panel);
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 14px;
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--muted);
  border-bottom: 1px solid var(--line);
  position: sticky;
  top: 0;
  background: var(--bg-panel);
  z-index: 1;
}

.count {
  font-size: 0.72rem;
  color: var(--muted-2);
}

.file {
  display: grid;
  grid-template-columns: 18px 1fr auto;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 9px 12px;
  border: none;
  border-bottom: 1px solid var(--line);
  background: transparent;
  text-align: left;
  cursor: pointer;
  color: var(--ink);
}

.file:hover {
  background: var(--bg-1);
}

.file.active {
  background: var(--accent-dim);
}

.status {
  font-size: 0.68rem;
  font-weight: 700;
  font-family: var(--mono);
  color: var(--muted);
}

.status[data-status='added'] {
  color: var(--success);
}

.status[data-status='deleted'] {
  color: var(--danger);
}

.path {
  font-size: 0.75rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stats {
  font-size: 0.68rem;
  display: flex;
  gap: 4px;
}

.stats em {
  font-style: normal;
}

.add {
  color: var(--success);
}

.del {
  color: var(--danger);
}

.empty {
  padding: 24px 14px;
  color: var(--muted);
  font-size: 0.82rem;
  text-align: center;
}
</style>
