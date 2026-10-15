<template>
  <div class="pr-summary surface fade-up">
    <div class="head">
      <div>
        <p class="eyebrow">交付摘要</p>
        <h2>{{ result.title || 'Pull Request' }}</h2>
      </div>
      <StatusBadge :status="statusTone" :label="statusLabel" />
    </div>

    <dl class="meta">
      <div v-if="repo">
        <dt>仓库</dt>
        <dd class="mono">{{ repo }}</dd>
      </div>
      <div v-if="result.pr_url">
        <dt>PR 链接</dt>
        <dd>
          <a :href="result.pr_url" target="_blank" rel="noreferrer">{{ result.pr_url }}</a>
        </dd>
      </div>
      <div v-if="result.compare_url">
        <dt>Compare</dt>
        <dd>
          <a :href="result.compare_url" target="_blank" rel="noreferrer">查看分支对比</a>
        </dd>
      </div>
    </dl>

    <p class="note">
      PR 创建成功表示变更已推送到 GitHub；测试 / 构建 / 类型检查的结果请以 PR Checks 与 Agent
      执行日志为准，未执行的检查不会标记为通过。
    </p>

    <p v-if="result.hint" class="hint">{{ result.hint }}</p>

    <div class="actions">
      <a v-if="result.pr_url" class="btn btn-primary" :href="result.pr_url" target="_blank" rel="noreferrer">
        查看 PR
        <AppIcon name="external" :size="14" />
      </a>
      <RouterLink class="btn" to="/approvals">返回任务 / 历史</RouterLink>
      <RouterLink class="btn btn-ghost" to="/chat">继续修改</RouterLink>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { ResultCard } from '../stores/chat'
import AppIcon from './ui/AppIcon.vue'
import StatusBadge from './ui/StatusBadge.vue'

const props = defineProps<{
  result: ResultCard
  repo?: string
}>()

const hasPr = computed(() => !!props.result.pr_url)
const statusTone = computed(() => (hasPr.value ? 'completed' : 'creating_pr'))
const statusLabel = computed(() => (hasPr.value ? 'PR 已创建' : '处理中'))
</script>

<style scoped>
.pr-summary {
  padding: 20px;
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}

.eyebrow {
  margin: 0 0 4px;
  font-size: 0.72rem;
  font-weight: 600;
  color: var(--accent);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

h2 {
  margin: 0;
  font-size: 1.15rem;
  letter-spacing: -0.02em;
}

.meta {
  margin: 0 0 14px;
  display: grid;
  gap: 10px;
}

.meta div {
  display: grid;
  grid-template-columns: 88px 1fr;
  gap: 8px;
  font-size: 0.85rem;
}

dt {
  color: var(--muted);
  margin: 0;
}

dd {
  margin: 0;
  word-break: break-all;
}

.note,
.hint {
  margin: 0 0 10px;
  font-size: 0.8rem;
  color: var(--muted);
  line-height: 1.5;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}
</style>
