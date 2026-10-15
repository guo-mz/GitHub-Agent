<template>
  <div class="empty fade-up">
    <div v-if="icon" class="icon-wrap">
      <AppIcon :name="icon" :size="22" />
    </div>
    <h3 v-if="title">{{ title }}</h3>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.default" class="actions">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue'
import type { IconName } from '../icons'

defineProps<{
  title?: string
  description?: string
  icon?: IconName
}>()
</script>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 48px 24px;
  border: 1px dashed var(--line);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  color: var(--muted);
}

.icon-wrap {
  width: 44px;
  height: 44px;
  border-radius: 10px;
  background: var(--accent-dim);
  color: var(--accent);
  display: grid;
  place-items: center;
  margin-bottom: 14px;
}

h3 {
  margin: 0 0 6px;
  font-size: 1rem;
  font-weight: 600;
  color: var(--ink);
}

p {
  margin: 0;
  max-width: 42ch;
  line-height: 1.55;
  font-size: 0.88rem;
}

.actions {
  margin-top: 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}
</style>
