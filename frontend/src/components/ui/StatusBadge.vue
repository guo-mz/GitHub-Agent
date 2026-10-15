<template>
  <span class="badge" :data-tone="tone">{{ label }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  status?: string
  label?: string
}>()

const map: Record<string, { label: string; tone: string }> = {
  queued: { label: '排队中', tone: 'neutral' },
  analyzing: { label: '分析中', tone: 'info' },
  planning: { label: '规划中', tone: 'info' },
  editing: { label: '编辑中', tone: 'info' },
  checking: { label: '检查中', tone: 'info' },
  verifying: { label: '验证中', tone: 'info' },
  repairing: { label: '修复中', tone: 'warn' },
  awaiting_approval: { label: '待审批', tone: 'warn' },
  awaiting_coding: { label: '计划已确认，等待执行', tone: 'warn' },
  diff_approved: { label: 'Diff 已确认，等待发布', tone: 'warn' },
  publishing: { label: '提交中', tone: 'info' },
  creating_pr: { label: '创建 PR', tone: 'info' },
  succeeded: { label: '已成功', tone: 'success' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'neutral' },
  running: { label: '执行中', tone: 'info' },
  ready: { label: '就绪', tone: 'neutral' },
  approved: { label: '已批准', tone: 'success' },
  rejected: { label: '已拒绝', tone: 'danger' },
  pending: { label: '待处理', tone: 'warn' },
  success: { label: '成功', tone: 'success' },
  high: { label: '高风险', tone: 'warn' },
  critical: { label: '严重', tone: 'danger' },
  medium: { label: '中风险', tone: 'warn' },
  low: { label: '低风险', tone: 'neutral' },
  enter: { label: '进行中', tone: 'info' },
  done: { label: '完成', tone: 'success' },
}

const resolved = computed(() => {
  const key = (props.status || '').toLowerCase()
  return map[key] || { label: props.label || props.status || '—', tone: 'neutral' }
})

const label = computed(() => props.label || resolved.value.label)
const tone = computed(() => resolved.value.tone)
</script>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.02em;
  border: 1px solid var(--line);
  background: var(--bg-1);
  color: var(--muted);
  white-space: nowrap;
}

.badge[data-tone='info'] {
  background: var(--accent-dim);
  border-color: rgba(98, 88, 232, 0.25);
  color: var(--accent);
}

.badge[data-tone='success'] {
  background: var(--success-dim);
  border-color: rgba(35, 134, 91, 0.28);
  color: var(--success);
}

.badge[data-tone='danger'] {
  background: var(--danger-dim);
  border-color: rgba(200, 77, 85, 0.28);
  color: var(--danger);
}

.badge[data-tone='warn'] {
  background: var(--warn-dim);
  border-color: rgba(180, 83, 9, 0.28);
  color: var(--warn);
}
</style>
