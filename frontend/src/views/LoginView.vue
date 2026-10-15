<template>
  <section class="login">
    <div class="login-bg" aria-hidden="true" />
    <div class="panel fade-up">
      <div class="brand">
        <div class="logo" aria-hidden="true">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
            <rect x="3" y="3" width="18" height="18" rx="5" fill="currentColor" opacity="0.14" />
            <path
              d="M8 12.5l2.5 2.5L16 9"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </div>
        <div>
          <h1>GitHub <em>Agent</em></h1>
          <p class="sub">操作员登录 · 本地单密钥认证</p>
        </div>
      </div>

      <p class="lead">
        输入与后端 <code>OPERATOR_API_TOKEN</code> 一致的密钥，进入工作台。这不是多用户账号体系，而是本机操作员门禁。
      </p>

      <form class="form" @submit.prevent="onSubmit">
        <label class="field">
          <span>操作员 Token</span>
          <div class="input-wrap">
            <input
              v-model="token"
              class="input"
              :type="show ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="粘贴 OPERATOR_API_TOKEN"
              :disabled="busy"
              autofocus
            />
            <button class="toggle" type="button" :disabled="busy" @click="show = !show">
              {{ show ? '隐藏' : '显示' }}
            </button>
          </div>
        </label>

        <p v-if="error" class="err">{{ error }}</p>
        <p v-if="hint" class="hint">{{ hint }}</p>

        <button class="btn btn-primary submit" type="submit" :disabled="busy || !token.trim()">
          {{ busy ? '校验中…' : '进入工作台' }}
        </button>
      </form>

      <div class="extras">
        <button
          v-if="authMode === 'insecure_open'"
          class="btn btn-ghost"
          type="button"
          :disabled="busy"
          @click="enterWithoutToken"
        >
          跳过（服务端 insecure_open）
        </button>
        <a class="btn btn-ghost" :href="githubOAuthUrl" @click="onGithubClick">
          GitHub 授权仓库访问
        </a>
      </div>

      <ul class="notes">
        <li>Token 保存在本机 <code>localStorage</code>（开发用途；XSS 可窃取，勿用于公网）。</li>
        <li>在 <code>backend/.env</code> 配置 <code>OPERATOR_API_TOKEN</code> 后重启后端。</li>
        <li v-if="backendLabel">后端：{{ backendLabel }}</li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  fetchHealth,
  getApiBase,
  setOperatorToken,
  verifyOperatorToken,
} from '../api'

const router = useRouter()
const route = useRoute()

const token = ref('')
const show = ref(false)
const busy = ref(false)
const error = ref('')
const hint = ref('')
const authMode = ref('')
const backendOk = ref<boolean | null>(null)

const githubOAuthUrl = computed(() => `${getApiBase()}/api/auth/github`)
const backendLabel = computed(() => {
  if (backendOk.value === null) return ''
  if (!backendOk.value) return '不可达 · 请先启动 API'
  return `已连接 · auth_mode=${authMode.value || '?'}`
})

onMounted(async () => {
  try {
    const h = await fetchHealth()
    backendOk.value = h.status === 'ok'
    authMode.value = h.auth_mode || ''
    if (authMode.value === 'fail_closed') {
      hint.value = '服务端未配置 OPERATOR_API_TOKEN，敏感接口会拒绝请求。请先写好 .env。'
    } else if (authMode.value === 'insecure_open') {
      hint.value = '当前为 insecure_open：可临时跳过 Token（仅本地调试）。'
    }
  } catch {
    backendOk.value = false
    hint.value = '无法探测 /health，请确认 VITE_API_BASE 与后端端口。'
  }
})

async function onSubmit() {
  error.value = ''
  busy.value = true
  try {
    const raw = token.value.trim()
    const result = await verifyOperatorToken(raw)
    if (!result.ok) {
      error.value = result.detail || '登录失败'
      return
    }
    setOperatorToken(raw)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect || '/')
  } finally {
    busy.value = false
  }
}

async function enterWithoutToken() {
  error.value = ''
  await router.replace('/')
}

function onGithubClick(e: MouseEvent) {
  // OAuth 换的是 GitHub access_token，不能替代操作员门禁；仅作仓库授权入口
  if (!backendOk.value) {
    e.preventDefault()
    error.value = '后端不可达，无法发起 GitHub OAuth'
  }
}
</script>

<style scoped>
.login {
  position: relative;
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 32px 16px;
}

.login-bg {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(900px 420px at 12% -10%, rgba(98, 88, 232, 0.12), transparent 60%),
    radial-gradient(700px 360px at 100% 0%, rgba(15, 118, 110, 0.08), transparent 55%),
    linear-gradient(180deg, #f7f6f4 0%, var(--bg-page) 55%, #f3f2f0 100%);
  pointer-events: none;
}

.panel {
  position: relative;
  width: min(440px, 100%);
  background: var(--bg-panel);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  padding: 28px 26px 22px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}

.logo {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  display: grid;
  place-items: center;
  color: var(--accent);
  background: var(--accent-dim);
  border: 1px solid rgba(98, 88, 232, 0.18);
}

h1 {
  margin: 0;
  font-size: 1.35rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.2;
}

h1 em {
  font-style: normal;
  color: var(--accent);
}

.sub {
  margin: 2px 0 0;
  font-size: 0.78rem;
  color: var(--muted);
}

.lead {
  margin: 0 0 18px;
  font-size: 0.88rem;
  line-height: 1.55;
  color: var(--muted);
}

.lead code,
.notes code {
  font-family: var(--mono);
  font-size: 0.78em;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg-1);
  color: var(--ink);
}

.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--ink);
}

.input-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-1);
  padding: 2px 2px 2px 10px;
}

.input-wrap:focus-within {
  box-shadow: var(--focus-ring);
  border-color: rgba(98, 88, 232, 0.45);
  background: #fff;
}

.input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  outline: none;
  padding: 10px 0;
  font-family: var(--mono);
  font-size: 0.84rem;
  color: var(--ink);
}

.toggle {
  border: none;
  background: transparent;
  color: var(--muted);
  font-size: 0.75rem;
  padding: 8px 10px;
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.toggle:hover {
  color: var(--ink);
  background: var(--bg-2);
}

.submit {
  width: 100%;
  min-height: 42px;
  margin-top: 4px;
}

.hint {
  margin: 0;
  font-size: 0.8rem;
  color: var(--warn);
  line-height: 1.45;
}

.extras {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.extras .btn {
  flex: 1;
  min-width: 140px;
  text-decoration: none;
  font-size: 0.8rem;
}

.notes {
  margin: 18px 0 0;
  padding: 12px 12px 12px 28px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 0.75rem;
  line-height: 1.55;
}

.notes li + li {
  margin-top: 4px;
}

.fade-up {
  animation: fadeUp 0.35s ease both;
}

@keyframes fadeUp {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

@media (max-width: 520px) {
  .panel {
    padding: 22px 18px 18px;
  }
}
</style>
