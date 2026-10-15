<template>
  <header class="topbar">
    <div class="left">
      <button class="btn btn-ghost menu" type="button" aria-label="打开菜单" @click="$emit('menu')">
        <AppIcon name="menu" :size="18" />
      </button>
      <nav class="crumbs" aria-label="面包屑">
        <span class="root">GitHub Agent</span>
        <AppIcon name="chevronRight" :size="14" class="sep" />
        <span class="current">{{ title }}</span>
      </nav>
    </div>
    <div class="right">
      <div v-if="showSearch" class="search">
        <AppIcon name="search" :size="15" />
        <input
          class="search-input"
          type="search"
          :value="search"
          placeholder="搜索任务或仓库…"
          @input="$emit('update:search', ($event.target as HTMLInputElement).value)"
        />
      </div>
      <RouterLink to="/approvals" class="icon-btn" title="待审批 / PR" aria-label="通知">
        <AppIcon name="bell" :size="17" />
        <span v-if="pending > 0" class="dot">{{ pending > 9 ? '9+' : pending }}</span>
      </RouterLink>
      <div class="avatar" :title="repo || '账户'" aria-hidden="true">
        {{ initials }}
      </div>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { fetchPendingApprovals } from '../../api'
import { useChatStore } from '../../stores/chat'
import AppIcon from '../ui/AppIcon.vue'

defineProps<{
  search?: string
  showSearch?: boolean
}>()
defineEmits<{
  (e: 'menu'): void
  (e: 'update:search', v: string): void
}>()

const route = useRoute()
const chat = useChatStore()
const pending = ref(0)

const titles: Record<string, string> = {
  home: '工作空间',
  chat: '任务执行',
  review: '代码审查',
  plan: '确认计划',
  repo: 'GitHub 仓库',
  approvals: 'Pull Requests',
  editor: '代码编辑器',
  eval: '评测 / 设置',
  login: '登录',
}

const title = computed(() => titles[String(route.name || '')] || '工作台')
const repo = computed(() => chat.repo)
const initials = computed(() => {
  const r = repo.value || 'GA'
  const part = r.split('/')[0] || 'GA'
  return part.slice(0, 2).toUpperCase()
})

onMounted(async () => {
  try {
    const items = await fetchPendingApprovals()
    pending.value = Array.isArray(items) ? items.length : 0
  } catch {
    pending.value = 0
  }
})
</script>

<style scoped>
.topbar {
  height: var(--topbar-h);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 18px;
  border-bottom: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(10px);
}

.left,
.right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.menu {
  display: none;
  padding: 6px;
  min-height: 0;
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  font-size: 0.875rem;
}

.root {
  color: var(--muted);
}

.sep {
  color: var(--muted-2);
}

.current {
  font-weight: 600;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.search {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-1);
  color: var(--muted);
  min-width: 180px;
}

.search-input {
  border: none;
  background: transparent;
  outline: none;
  color: var(--ink);
  width: 160px;
  font-size: 0.82rem;
}

.icon-btn {
  position: relative;
  width: 34px;
  height: 34px;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  color: var(--muted);
  text-decoration: none;
  border: 1px solid transparent;
}

.icon-btn:hover {
  background: var(--bg-1);
  color: var(--ink);
  text-decoration: none;
}

.dot {
  position: absolute;
  top: 2px;
  right: 2px;
  min-width: 14px;
  height: 14px;
  padding: 0 3px;
  border-radius: 999px;
  background: var(--danger);
  color: #fff;
  font-size: 0.58rem;
  font-weight: 700;
  display: grid;
  place-items: center;
}

.avatar {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 0.7rem;
  font-weight: 700;
  display: grid;
  place-items: center;
  border: 1px solid rgba(98, 88, 232, 0.2);
}

@media (max-width: 900px) {
  .menu {
    display: inline-flex;
  }

  .search {
    display: none;
  }
}
</style>
