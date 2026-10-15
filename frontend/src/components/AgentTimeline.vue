<template>
  <div v-if="visible.length" class="tl" aria-label="Agent 状态机">
    <div v-if="hidden > 0" class="earlier mono">+{{ hidden }} earlier</div>
    <ol class="steps">
      <li
        v-for="(n, i) in visible"
        :key="`${n.name}-${i}-${n.status}`"
        class="step"
        :data-status="n.status"
        :title="n.name"
      >
        <span class="idx mono">{{ startIndex + i }}</span>
        <span class="rail" aria-hidden="true" />
        <div class="body">
          <span class="label">{{ n.label || n.name }}</span>
          <span v-if="n.status === 'enter'" class="live">进行中</span>
          <span v-else class="done-tag">完成</span>
        </div>
      </li>
    </ol>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { TimelineNode } from '../stores/chat'

const props = defineProps<{ nodes: TimelineNode[] }>()

const MAX = 8

const visible = computed(() => {
  const list = props.nodes || []
  return list.length > MAX ? list.slice(-MAX) : list
})

const hidden = computed(() => Math.max(0, (props.nodes?.length || 0) - MAX))

const startIndex = computed(() => hidden.value + 1)
</script>

<style scoped>
.tl {
  margin: 0 0 10px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-1);
}

.earlier {
  font-size: 0.68rem;
  color: var(--muted);
  margin-bottom: 8px;
  opacity: 0.8;
}

.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.step {
  display: grid;
  grid-template-columns: 22px 14px 1fr;
  gap: 4px;
  align-items: stretch;
  min-height: 28px;
}

.idx {
  font-size: 0.68rem;
  color: var(--muted);
  text-align: right;
  padding-top: 4px;
  opacity: 0.7;
}

.rail {
  position: relative;
  width: 10px;
  justify-self: center;
}

.rail::before {
  content: '';
  position: absolute;
  left: 50%;
  top: 8px;
  width: 8px;
  height: 8px;
  margin-left: -4px;
  border-radius: 50%;
  background: var(--muted);
  border: 1px solid var(--line);
  z-index: 1;
}

.step:not(:last-child) .rail::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 16px;
  bottom: -4px;
  width: 1px;
  margin-left: -0.5px;
  background: var(--line);
}

.step[data-status='done'] .rail::before {
  background: var(--success);
  border-color: rgba(35, 134, 91, 0.4);
}

.step[data-status='enter'] .rail::before {
  background: var(--accent);
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(98, 88, 232, 0.18);
  animation: pulse 1.2s infinite;
}

.body {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 2px 0 10px;
  min-width: 0;
}

.label {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step[data-status='enter'] .label {
  color: var(--accent);
}

.live,
.done-tag {
  flex-shrink: 0;
  font-size: 0.65rem;
  letter-spacing: 0.04em;
  color: var(--muted);
}

.live {
  color: var(--accent);
}
</style>
