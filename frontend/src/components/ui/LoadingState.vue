<template>
  <div class="loading" role="status" :aria-label="label">
    <span class="spinner" />
    <span v-if="label">{{ label }}</span>
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{ label?: string }>(), { label: '加载中…' })
</script>

<style scoped>
.loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 32px;
  color: var(--muted);
  font-size: 0.88rem;
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid var(--line-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
</style>
