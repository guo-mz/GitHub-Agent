<template>
  <div ref="root" class="stream">
    <MessageBubble v-for="m in messages" :key="m.id" :message="m" />

    <div v-if="!messages.length" class="empty fade-up surface">
      <p class="eyebrow">开始一次研发任务</p>
      <h2>用自然语言驱动 GitHub</h2>
      <p class="desc">
        Issue/PR、智能审查、安全扫描、按审查修复并提 PR。高危写操作人工审批，拒绝无副作用。
      </p>
      <div class="prompts">
        <button
          v-for="p in prompts"
          :key="p"
          type="button"
          class="prompt"
          @click="$emit('prompt', p)"
        >
          {{ p }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ChatMessage } from '../stores/chat'
import MessageBubble from './MessageBubble.vue'

const props = defineProps<{ messages: ChatMessage[] }>()
defineEmits<{ (e: 'prompt', text: string): void }>()

const root = ref<HTMLElement | null>(null)

const prompts = [
  '① 审查 README.md，给出风险等级和优先修复清单',
  '② 按上次审查结果，在新分支修复并创建 PR（走人工审批）',
  '对 README.md 做安全扫描',
  '列出 open PR，并审查编号最大的那个（若没有就说明）',
  '列出当前仓库最近的 open issues',
]

watch(
  () =>
    props.messages
      .map(
        (m) =>
          `${m.id}:${m.content?.length}:${m.tools?.length}:${m.timeline?.length}:${m.streaming}`,
      )
      .join('|'),
  async () => {
    await nextTick()
    const el = root.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<style scoped>
.stream {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 2px 2px 8px;
  scroll-behavior: smooth;
}

.empty {
  margin: auto;
  text-align: left;
  max-width: 520px;
  padding: 28px 24px;
}

.eyebrow {
  margin: 0 0 8px;
  color: var(--accent);
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

h2 {
  margin: 0 0 10px;
  font-size: 1.35rem;
  letter-spacing: -0.03em;
  font-weight: 650;
}

.desc {
  margin: 0 0 18px;
  color: var(--muted);
  line-height: 1.6;
  font-size: 0.92rem;
}

.prompts {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.prompt {
  text-align: left;
  border: 1px solid var(--line);
  background: var(--bg-1);
  color: var(--ink);
  border-radius: 8px;
  padding: 11px 12px;
  cursor: pointer;
  font-size: 0.88rem;
  line-height: 1.4;
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
}

.prompt:hover {
  border-color: rgba(98, 88, 232, 0.35);
  background: var(--accent-dim);
  transform: translateX(2px);
}
</style>
