<template>
  <aside class="sidebar" :class="{ open: mobileOpen }">
    <div class="brand">
      <div class="logo" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <rect x="3" y="3" width="18" height="18" rx="5" fill="currentColor" opacity="0.12" />
          <path
            d="M8 12.5l2.5 2.5L16 9"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </div>
      <div class="brand-text">
        <div class="brand-title">GitHub <em>Agent</em></div>
        <div class="brand-sub">AI 开发工作台</div>
      </div>
      <button class="btn btn-ghost close-mobile" type="button" aria-label="关闭菜单" @click="$emit('close')">
        <AppIcon name="x" :size="18" />
      </button>
    </div>

    <RouterLink class="new-task" to="/" @click="$emit('close')">
      <AppIcon name="plus" :size="16" />
      新建任务
    </RouterLink>

    <nav class="nav" aria-label="主导航">
      <RouterLink
        v-for="item in primaryNav"
        :key="item.to"
        :to="item.to"
        class="nav-item"
        @click="$emit('close')"
      >
        <AppIcon :name="item.icon" :size="17" />
        <span>{{ item.label }}</span>
        <span v-if="item.badge" class="nav-badge">{{ item.badge }}</span>
      </RouterLink>
    </nav>

    <div v-if="recentRepos.length" class="recent">
      <div class="recent-label">最近仓库</div>
      <button
        v-for="r in recentRepos"
        :key="r"
        type="button"
        class="recent-item mono"
        :title="r"
        @click="selectRepo(r)"
      >
        <AppIcon name="repo" :size="14" />
        <span>{{ r }}</span>
      </button>
    </div>

    <div class="sidebar-foot">
      <div class="health" :title="healthHint">
        <span :class="['dot', healthOk ? 'on' : 'off']" />
        <span class="mono">{{ healthLabel }}</span>
      </div>
      <div class="chips mono">
        <span :data-on="!!health?.redis">Redis</span>
        <span :data-on="health?.checkpointer === 'redis'" :title="health?.checkpointer || ''">
          CKPT
        </span>
        <span :data-on="!!health?.rag" :title="health?.rag_mode || ''">RAG</span>
      </div>
      <button class="logout" type="button" title="清除本机操作员 Token" @click="logout">
        退出登录
      </button>
    </div>
  </aside>
  <div v-if="mobileOpen" class="scrim" @click="$emit('close')" />
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { clearOperatorToken, fetchHealth, fetchPendingApprovals } from '../../api'
import { useChatStore } from '../../stores/chat'
import AppIcon from '../ui/AppIcon.vue'
import type { IconName } from '../icons'

defineProps<{ mobileOpen?: boolean }>()
defineEmits<{ (e: 'close'): void }>()

const chat = useChatStore()
const router = useRouter()
const pendingCount = ref(0)
const health = ref<{
  status: string
  redis: boolean
  postgres: boolean
  rag?: boolean
  rag_mode?: string
  checkpointer?: string
} | null>(null)

const recentRepos = computed(() => {
  try {
    const raw = localStorage.getItem('ga_recent_repos')
    const list: string[] = raw ? JSON.parse(raw) : []
    const cur = chat.repo
    const merged = [cur, ...list.filter((r) => r && r !== cur)].slice(0, 5)
    return merged.filter(Boolean)
  } catch {
    return chat.repo ? [chat.repo] : []
  }
})

const primaryNav = computed(() => {
  const items: { to: string; label: string; icon: IconName; badge?: number }[] = [
    { to: '/', label: '工作空间', icon: 'home' },
    { to: '/chat', label: '任务执行', icon: 'chat' },
    { to: '/approvals', label: 'Pull Requests', icon: 'pr', badge: pendingCount.value || undefined },
    { to: '/repo', label: 'GitHub 仓库', icon: 'repo' },
    { to: '/editor', label: '代码编辑器', icon: 'editor' },
    { to: '/usage', label: 'AI 用量', icon: 'usage' },
    { to: '/eval', label: '评测 / 设置', icon: 'settings' },
  ]
  return items
})

const healthOk = computed(() => health.value?.status === 'ok')
const healthLabel = computed(() => {
  if (!health.value) return 'backend ?'
  return healthOk.value ? 'backend ok' : 'backend down'
})
const healthHint = computed(() => {
  const h = health.value
  if (!h) return '正在探测后端 /health'
  return `Redis ${h.redis ? 'on' : 'off'} · CKPT ${h.checkpointer || '?'} · RAG ${h.rag ? h.rag_mode || 'on' : 'off'}`
})

function selectRepo(r: string) {
  chat.setRepo(r)
  router.push('/repo')
}

function logout() {
  clearOperatorToken()
  router.push({ name: 'login' })
}

async function refresh() {
  try {
    health.value = await fetchHealth()
  } catch {
    health.value = { status: 'down', redis: false, postgres: false }
  }
  try {
    const items = await fetchPendingApprovals()
    pendingCount.value = Array.isArray(items) ? items.length : 0
  } catch {
    pendingCount.value = 0
  }
}

onMounted(() => {
  refresh()
  window.setInterval(refresh, 20000)
})
</script>

<style scoped>
.sidebar {
  width: var(--sidebar-w);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 12px;
  border-right: 1px solid var(--line);
  background: var(--bg-panel);
  height: 100%;
  overflow: hidden;
  z-index: 40;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 6px 8px;
}

.logo {
  width: 34px;
  height: 34px;
  border-radius: 9px;
  background: var(--accent-dim);
  color: var(--accent);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}

.brand-text {
  min-width: 0;
  flex: 1;
}

.brand-title {
  font-weight: 700;
  font-size: 0.98rem;
  letter-spacing: -0.03em;
  line-height: 1.2;
}

.brand-title em {
  font-style: normal;
  color: var(--accent);
}

.brand-sub {
  color: var(--muted);
  font-size: 0.68rem;
  margin-top: 2px;
}

.close-mobile {
  display: none;
  padding: 6px;
  min-height: 0;
}

.new-task {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin: 0 4px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: var(--accent-ink);
  font-weight: 600;
  font-size: 0.875rem;
  text-decoration: none;
  transition: background var(--transition);
  box-shadow: 0 1px 2px rgba(98, 88, 232, 0.25);
}

.new-task:hover {
  background: var(--accent-hover);
  text-decoration: none;
  color: var(--accent-ink);
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 4px 0;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  color: var(--muted);
  font-size: 0.875rem;
  font-weight: 500;
  text-decoration: none;
  border: 1px solid transparent;
  transition: background var(--transition), color var(--transition), border-color var(--transition);
}

.nav-item:hover {
  color: var(--ink);
  background: var(--bg-1);
  text-decoration: none;
}

.nav-item.router-link-active {
  color: var(--accent);
  background: var(--accent-dim);
  border-color: rgba(98, 88, 232, 0.15);
}

.nav-badge {
  margin-left: auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--warn-dim);
  color: var(--warn);
  font-size: 0.68rem;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.recent {
  padding: 8px 4px 4px;
  border-top: 1px solid var(--line);
}

.recent-label {
  font-size: 0.68rem;
  font-weight: 600;
  color: var(--muted-2);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0 8px 6px;
}

.recent-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  border: none;
  background: transparent;
  color: var(--muted);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 0.75rem;
  text-align: left;
}

.recent-item span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-item:hover {
  background: var(--bg-1);
  color: var(--ink);
}

.sidebar-foot {
  padding: 10px 8px 4px;
  border-top: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 0.7rem;
  color: var(--muted);
}

.health {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--muted-2);
}

.dot.on {
  background: var(--success);
  box-shadow: 0 0 0 3px var(--success-dim);
}

.dot.off {
  background: var(--danger);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.chips span {
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid var(--line);
  opacity: 0.55;
  font-size: 0.65rem;
}

.chips span[data-on='true'] {
  opacity: 1;
  border-color: rgba(35, 134, 91, 0.35);
  color: var(--success);
}

.logout {
  margin-top: 4px;
  width: 100%;
  border: 1px solid var(--line);
  background: transparent;
  color: var(--muted);
  border-radius: var(--radius-sm);
  padding: 7px 10px;
  font-size: 0.75rem;
  cursor: pointer;
  text-align: left;
}

.logout:hover {
  color: var(--danger);
  border-color: rgba(200, 77, 85, 0.35);
  background: var(--danger-dim);
}

.scrim {
  display: none;
}

@media (max-width: 900px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    transform: translateX(-105%);
    transition: transform 0.2s ease;
    box-shadow: var(--shadow-lg);
  }

  .sidebar.open {
    transform: translateX(0);
  }

  .close-mobile {
    display: inline-flex;
  }

  .scrim {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(28, 25, 23, 0.35);
    z-index: 35;
  }
}
</style>
