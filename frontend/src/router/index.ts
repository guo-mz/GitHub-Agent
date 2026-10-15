import { createRouter, createWebHistory } from 'vue-router'
import { fetchHealth, getOperatorToken } from '../api'
import HomeView from '../views/HomeView.vue'
import ChatView from '../views/ChatView.vue'
import RepoView from '../views/RepoView.vue'
import ApprovalsView from '../views/ApprovalsView.vue'
import EditorView from '../views/EditorView.vue'
import EvalView from '../views/EvalView.vue'
import UsageView from '../views/UsageView.vue'
import ReviewView from '../views/ReviewView.vue'
import PlanConfirmView from '../views/PlanConfirmView.vue'
import LoginView from '../views/LoginView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { public: true, hideShell: true },
    },
    { path: '/', name: 'home', component: HomeView },
    { path: '/chat', name: 'chat', component: ChatView },
    { path: '/plan', name: 'plan', component: PlanConfirmView },
    { path: '/review', name: 'review', component: ReviewView },
    { path: '/editor', name: 'editor', component: EditorView },
    { path: '/repo', name: 'repo', component: RepoView },
    { path: '/approvals', name: 'approvals', component: ApprovalsView },
    { path: '/eval', name: 'eval', component: EvalView },
    { path: '/usage', name: 'usage', component: UsageView },
  ],
})

let cachedAuthMode: string | null = null
let authModeFetchedAt = 0

async function resolveAuthMode(): Promise<string> {
  const now = Date.now()
  if (cachedAuthMode && now - authModeFetchedAt < 30_000) {
    return cachedAuthMode
  }
  try {
    const h = await fetchHealth()
    cachedAuthMode = h.auth_mode || 'fail_closed'
    authModeFetchedAt = now
    return cachedAuthMode
  } catch {
    return cachedAuthMode || 'fail_closed'
  }
}

router.beforeEach(async (to) => {
  if (to.meta.public) {
    if (to.name === 'login' && getOperatorToken()) {
      const redirect = typeof to.query.redirect === 'string' ? to.query.redirect : '/'
      return redirect || '/'
    }
    return true
  }

  if (getOperatorToken()) return true

  const mode = await resolveAuthMode()
  if (mode === 'insecure_open') return true

  return {
    name: 'login',
    query: { redirect: to.fullPath },
  }
})

export default router
