<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="icons[name]" />
  </svg>
</template>

<script setup lang="ts">
import { icons, type IconName } from '../icons'

withDefaults(
  defineProps<{
    name: IconName
    size?: number | string
  }>(),
  { size: 18 },
)
</script>

<style scoped>
.app-icon {
  flex-shrink: 0;
  display: block;
}
</style>
