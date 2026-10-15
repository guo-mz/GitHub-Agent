<template>
  <section class="editor-page">
    <div v-if="!canWrite" class="token-banner">
      <div class="token-banner-head">
        <strong>推荐：添加 Deploy Key（一次，约 30 秒）</strong>
        <span class="perm mono">{{ permLabel }}</span>
      </div>
      <ol class="token-steps">
        <li>点「生成/显示公钥」，复制整行</li>
        <li>
          打开
          <a v-if="deployAddUrl" :href="deployAddUrl" target="_blank" rel="noreferrer">仓库 Deploy keys</a>
          <span v-else>仓库 Settings → Deploy keys</span>
          → Add deploy key
        </li>
        <li>Title: <code>github-agent</code>，粘贴公钥，<b>勾选 Allow write access</b></li>
        <li>回来点「检测 Deploy Key」→ 通过后即可保存上传</li>
      </ol>
      <textarea v-if="deployPubKey" class="field mono pubkey" readonly rows="2" :value="deployPubKey" />
      <div class="token-row">
        <button class="btn" :disabled="busy" @click="loadDeployKey">生成/显示公钥</button>
        <button class="btn" :disabled="busy || !deployPubKey" @click="copyDeployKey">复制公钥</button>
        <button class="btn btn-primary" :disabled="busy" @click="checkDeployKey">检测 Deploy Key</button>
        <button class="btn" :disabled="busy" @click="showTokenAlt = !showTokenAlt">
          {{ showTokenAlt ? '收起 Token 方案' : '改用 Token' }}
        </button>
      </div>
      <div v-if="showTokenAlt" class="token-alt">
        <div class="token-row">
          <input
            v-model="tokenInput"
            class="field mono grow"
            type="password"
            autocomplete="off"
            placeholder="粘贴 github_pat_…（Contents 需 Read and write）"
            :disabled="busy"
          />
          <button class="btn btn-primary" :disabled="busy || !tokenInput.trim()" @click="applyToken">
            应用 Token
          </button>
        </div>
      </div>
    </div>
    <div v-else class="token-ok">
      <span>可写（{{ writeVia }}{{ permLogin ? ' · ' + permLogin : '' }}）</span>
      <button class="btn linkish" type="button" @click="showTokenPanel = !showTokenPanel">
        {{ showTokenPanel ? '收起' : '权限设置' }}
      </button>
    </div>
    <div v-if="showTokenPanel && canWrite" class="token-banner compact">
      <div class="token-row">
        <button class="btn" :disabled="busy" @click="loadDeployKey">Deploy Key</button>
        <button class="btn" :disabled="busy" @click="checkPerms">重新检测</button>
        <input
          v-model="tokenInput"
          class="field mono grow"
          type="password"
          autocomplete="off"
          placeholder="更换 Token（可选）"
          :disabled="busy"
        />
        <button class="btn btn-primary" :disabled="busy || !tokenInput.trim()" @click="applyToken">
          应用 Token
        </button>
      </div>
      <textarea v-if="deployPubKey" class="field mono pubkey" readonly rows="2" :value="deployPubKey" />
    </div>

    <div class="topbar">
      <div class="fields">
        <label>
          <span>仓库</span>
          <input v-model="ownerRepo" class="field mono" placeholder="owner/repo" />
        </label>
        <label>
          <span>分支</span>
          <input v-model="branch" class="field mono" placeholder="main" />
        </label>
        <label class="grow">
          <span>文件路径</span>
          <input v-model="path" class="field mono" placeholder="README.md" @keydown.enter="loadFile" />
        </label>
      </div>
      <div class="actions">
        <button class="btn" :disabled="busy" @click="loadTree">刷新树</button>
        <button class="btn btn-primary" :disabled="busy || !path" @click="loadFile">加载</button>
      </div>
    </div>

    <div class="workspace">
      <aside class="file-tree">
        <p class="tree-title">文件</p>
        <button
          v-for="f in files"
          :key="f"
          type="button"
          class="file-item mono"
          :class="{ active: f === path }"
          @click="openPath(f)"
        >
          {{ f }}
        </button>
        <p v-if="!files.length" class="hint">加载仓库树后点选文件</p>
      </aside>

      <div class="editor-pane">
        <div class="ai-bar">
          <input
            v-model="instruction"
            class="field"
            placeholder="AI 指令，例如：把标题改成 Hello Agents，并加一段安装说明"
            :disabled="busy || streamingAi"
            @keydown.enter.exact.prevent="runAiEdit"
          />
          <button
            v-if="streamingAi"
            class="btn btn-danger"
            type="button"
            @click="stopAiEdit"
          >
            停止生成
          </button>
          <button
            v-else
            class="btn btn-primary"
            :disabled="busy || !contentLoaded"
            @click="runAiEdit"
          >
            AI 流式改代码
          </button>
        </div>

        <div ref="host" class="monaco-host" />

        <div class="save-bar">
          <input v-model="commitMsg" class="field grow" placeholder="commit message（仅本地说明）" />
          <button
            class="btn btn-primary"
            :disabled="busy || streamingAi"
            @click="saveFile"
            title="远程保存已禁用；请走对话多 Agent 审批发布"
          >
            尝试远程保存（已禁用）
          </button>
          <button
            class="btn"
            type="button"
            title="跳转对话，经计划/Diff/测试/发布审批后由 Publish Agent 写入"
            @click="goChatForPr"
          >
            对话提需求（安全路径）
          </button>
        </div>

        <p class="hitl-warn">
          编辑器直写 GitHub / open_pr 已由后端禁用。可浏览与本地改缓冲区，发布请走对话多 Agent 审批。
        </p>
        <p v-if="status" class="status" :class="{ err: isError }">{{ status }}</p>
        <p v-if="lastBranch" class="meta mono">
          branch {{ lastBranch }}
          <a v-if="prUrl" :href="prUrl" target="_blank" rel="noreferrer">打开 PR</a>
        </p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  applyGithubToken,
  fetchDeployKey,
  fetchGithubPermissions,
  fetchRepoFile,
  fetchRepoTree,
  saveRepoFile,
  streamAiEdit,
  verifyDeployKey,
} from '../api'
import { languageFromPath, loadMonaco } from '../monaco'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const router = useRouter()
const ownerRepo = ref(chat.repo || 'guo-mz/-')
const branch = ref('main')
const path = ref('README.md')
const files = ref<string[]>([])
const instruction = ref('')
const commitMsg = ref('chore: update via GitHub Agent editor')
/** 默认关闭：直提 PR 绕过 HITL；面试演示走「对话提 PR」 */
const status = ref('')
const isError = ref(false)
const busy = ref(false)
const dirty = ref(false)
const contentLoaded = ref(false)
const sha = ref('')
const lastBranch = ref('')
const prUrl = ref('')
const host = ref<HTMLElement | null>(null)
const tokenInput = ref('')
const canWrite = ref(false)
const permLogin = ref('')
const permDetail = ref('')
const showTokenPanel = ref(false)
const showTokenAlt = ref(false)
const deployPubKey = ref('')
const deployAddUrl = ref('')
const writeVia = ref('')
const streamingAi = ref(false)

let monacoApi: any = null
let editor: any = null
let original = ''
let aiAbort: AbortController | null = null

const permLabel = computed(() => {
  if (permDetail.value) return permDetail.value
  return canWrite.value ? '可写' : '当前 Token 只能读，不能创建分支/提交'
})

function parseOwnerRepo() {
  const [owner, repo] = ownerRepo.value.split('/')
  if (!owner || !repo) throw new Error('请输入 owner/repo')
  return { owner, repo }
}

function setStatus(msg: string, err = false) {
  status.value = typeof msg === 'string' ? msg : JSON.stringify(msg)
  isError.value = err
}

function detailFromError(e: any): string {
  const d = e?.response?.data?.detail
  if (!d) return e?.message || String(e)
  if (typeof d === 'string') return d
  if (d?.message) {
    const probe = d.probe?.detail ? ` — ${d.probe.detail}` : ''
    return `${d.message}${probe}`
  }
  return JSON.stringify(d)
}

async function checkPerms() {
  try {
    const { owner, repo } = parseOwnerRepo()
    const data = await fetchGithubPermissions(`${owner}/${repo}`)
    const patWrite = Boolean(data.can_create_branch)
    const deployOk = Boolean(data.deploy_key_ok)
    canWrite.value = Boolean(data.can_write ?? (patWrite || deployOk))
    permLogin.value = data.login || ''
    permDetail.value = data.detail || ''
    writeVia.value = patWrite ? 'PAT' : deployOk ? 'Deploy Key' : ''
    if (!canWrite.value) {
      showTokenPanel.value = false
      setStatus(data.detail || '无写权限：请添加 Deploy Key（推荐）或更换 Token', true)
      if (!deployPubKey.value) await loadDeployKey()
    } else {
      setStatus(`可写（${writeVia.value}${data.login ? ' · ' + data.login : ''}）`)
    }
    return data
  } catch (e: any) {
    canWrite.value = false
    setStatus(detailFromError(e), true)
    return null
  }
}

async function loadDeployKey() {
  busy.value = true
  try {
    const { owner, repo } = parseOwnerRepo()
    const data = await fetchDeployKey(`${owner}/${repo}`)
    deployPubKey.value = data.public_key || ''
    deployAddUrl.value = data.add_url || ''
    if (data.verify?.ok) {
      canWrite.value = true
      writeVia.value = 'Deploy Key'
      setStatus('Deploy Key 已可用，可以保存上传')
    } else {
      setStatus('公钥已生成：复制后到 GitHub 添加，并勾选 Allow write access')
    }
  } catch (e: any) {
    setStatus(detailFromError(e), true)
  } finally {
    busy.value = false
  }
}

async function copyDeployKey() {
  if (!deployPubKey.value) return
  try {
    await navigator.clipboard.writeText(deployPubKey.value)
    setStatus('公钥已复制')
  } catch {
    setStatus('复制失败，请手动选中公钥复制', true)
  }
}

async function checkDeployKey() {
  busy.value = true
  try {
    if (!deployPubKey.value) await loadDeployKey()
    const { owner, repo } = parseOwnerRepo()
    const data = await verifyDeployKey(`${owner}/${repo}`)
    if (data.ok) {
      canWrite.value = true
      writeVia.value = 'Deploy Key'
      setStatus('Deploy Key 检测通过，可以保存上传')
    } else {
      canWrite.value = false
      setStatus(data.detail || data.raw || 'Deploy Key 未生效', true)
    }
  } catch (e: any) {
    setStatus(detailFromError(e), true)
  } finally {
    busy.value = false
  }
}

async function applyToken() {
  const token = tokenInput.value.trim()
  if (!token) return
  busy.value = true
  try {
    const { owner, repo } = parseOwnerRepo()
    const data = await applyGithubToken(token, `${owner}/${repo}`)
    canWrite.value = Boolean(data.ok && data.probe?.can_create_branch)
    permLogin.value = data.probe?.login || ''
    permDetail.value = data.probe?.detail || data.message || ''
    if (canWrite.value) {
      writeVia.value = 'PAT'
      tokenInput.value = ''
      showTokenPanel.value = false
      setStatus('Token 已应用，写权限正常，可以保存上传')
    } else {
      setStatus(data.message || data.probe?.detail || 'Token 仍无写权限，可用上方 Deploy Key', true)
    }
  } catch (e: any) {
    canWrite.value = false
    const d = e?.response?.data?.detail
    if (d?.probe) {
      permDetail.value = d.probe.detail || d.message || ''
      permLogin.value = d.probe.login || ''
    }
    setStatus(detailFromError(e), true)
  } finally {
    busy.value = false
  }
  if (canWrite.value && dirty.value && editor) {
    await saveFile()
  }
}

async function ensureEditor() {
  if (editor || !host.value) return
  monacoApi = await loadMonaco()
  editor = monacoApi.editor.create(host.value, {
    value: '',
    language: 'markdown',
    theme: 'vs',
    automaticLayout: true,
    minimap: { enabled: false },
    fontSize: 13,
    fontFamily: 'IBM Plex Mono, ui-monospace, monospace',
    scrollBeyondLastLine: false,
    wordWrap: 'on',
  })
  editor.onDidChangeModelContent(() => {
    dirty.value = editor.getValue() !== original
  })
}

async function loadTree() {
  busy.value = true
  try {
    const { owner, repo } = parseOwnerRepo()
    chat.setRepo(`${owner}/${repo}`)
    const data = await fetchRepoTree(owner, repo, branch.value || 'main')
    files.value = (data.nodes || [])
      .filter((n: any) => n.type === 'file')
      .map((n: any) => n.path)
      .slice(0, 400)
    setStatus(`已加载 ${files.value.length} 个文件`)
  } catch (e: any) {
    setStatus(detailFromError(e), true)
  } finally {
    busy.value = false
  }
}

async function loadFile() {
  if (!path.value.trim()) return
  busy.value = true
  try {
    await ensureEditor()
    const { owner, repo } = parseOwnerRepo()
    chat.setRepo(`${owner}/${repo}`)
    const data = await fetchRepoFile(owner, repo, path.value.trim(), branch.value || 'main')
    sha.value = data.sha
    original = data.content || ''
    editor.setValue(original)
    const lang = languageFromPath(path.value)
    const model = editor.getModel()
    if (model && monacoApi) monacoApi.editor.setModelLanguage(model, lang)
    contentLoaded.value = true
    dirty.value = false
    prUrl.value = ''
    lastBranch.value = ''
    setStatus(`已加载 ${path.value}（${data.size ?? original.length} bytes）`)
  } catch (e: any) {
    setStatus(detailFromError(e), true)
  } finally {
    busy.value = false
  }
}

async function openPath(p: string) {
  path.value = p
  await loadFile()
}

function stopAiEdit() {
  aiAbort?.abort()
  aiAbort = null
  streamingAi.value = false
  busy.value = false
  setStatus('已停止 AI 流式改写（缓冲区保留当前内容）')
}

async function runAiEdit() {
  if (!editor || !instruction.value.trim()) return
  const snapshot = editor.getValue()
  busy.value = true
  streamingAi.value = true
  aiAbort = new AbortController()
  let acc = ''
  editor.setValue('')
  setStatus('AI 流式生成中…')
  try {
    const { owner, repo } = parseOwnerRepo()
    await streamAiEdit(
      owner,
      repo,
      {
        path: path.value,
        content: snapshot,
        instruction: instruction.value.trim(),
      },
      (event, data) => {
        const ev = event || data.event
        if (ev === 'delta') {
          acc += data.data?.delta || ''
          editor.setValue(acc)
          dirty.value = true
        } else if (ev === 'done') {
          const finalText = data.data?.content ?? acc
          editor.setValue(finalText)
          dirty.value = editor.getValue() !== original
          setStatus(data.data?.detail || 'AI 流式完成，请确认后点「保存上传」')
        } else if (ev === 'error') {
          setStatus(data.data?.message || 'AI 流式失败', true)
          if (snapshot && !acc) editor.setValue(snapshot)
        }
      },
      aiAbort.signal,
    )
  } catch (e: any) {
    if (e?.name === 'AbortError') {
      setStatus('已停止 AI 流式改写')
    } else {
      setStatus(detailFromError(e), true)
      if (snapshot && !editor.getValue()) editor.setValue(snapshot)
    }
  } finally {
    streamingAi.value = false
    aiAbort = null
    busy.value = false
    dirty.value = editor ? editor.getValue() !== original : dirty.value
  }
}

function goChatForPr() {
  chat.setRepo(ownerRepo.value.trim())
  const prompt =
    `请修改仓库文件 \`${path.value}\`。` +
    (instruction.value ? `需求：${instruction.value}` : `需求：${commitMsg.value || '按编辑器当前意图更新该文件'}。`) +
    ` 请走计划确认 → Diff 审批 → 测试门禁 → 发布审批，禁止直写 main。`
  sessionStorage.setItem('ga_prefill', prompt)
  router.push('/chat')
}

async function saveFile() {
  if (!editor) return
  busy.value = true
  try {
    const { owner, repo } = parseOwnerRepo()
    // 后端已拒绝远程写入；此处调用仅用于展示明确错误，并引导走对话路径
    await saveRepoFile(owner, repo, {
      path: path.value,
      content: editor.getValue(),
      message: commitMsg.value || `chore: update ${path.value}`,
      base_branch: branch.value || 'main',
      sha: sha.value || undefined,
      open_pr: false,
    })
    setStatus('意外：远程保存成功（不应发生）。请检查后端安全策略。', true)
  } catch (e: any) {
    const msg = detailFromError(e)
    setStatus(
      msg || '远程保存已禁用。请使用「对话提需求（安全路径）」经审批后发布。',
      true,
    )
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await nextTick()
  try {
    await ensureEditor()
    await checkPerms()
    await loadTree()
    await loadFile()
  } catch (e: any) {
    setStatus(e?.message || String(e), true)
  }
})

onBeforeUnmount(() => {
  aiAbort?.abort()
  aiAbort = null
  editor?.dispose?.()
  editor = null
})

watch(ownerRepo, async () => {
  chat.setRepo(ownerRepo.value.trim())
  await checkPerms()
})
</script>

<style scoped>
.editor-page {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.token-banner {
  flex-shrink: 0;
  border: 1px solid #b45309;
  background: rgba(180, 83, 9, 0.12);
  border-radius: var(--radius);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.token-banner.compact {
  border-color: var(--line);
  background: var(--bg-panel);
}

.token-banner-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: baseline;
  justify-content: space-between;
}

.token-banner-head strong {
  color: var(--warn);
  font-size: 0.92rem;
}

.perm {
  color: var(--muted);
  font-size: 0.75rem;
}

.token-steps {
  margin: 0;
  padding-left: 18px;
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.45;
}

.token-steps a {
  color: var(--accent);
}

.token-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.pubkey {
  width: 100%;
  resize: vertical;
  font-size: 0.72rem;
  line-height: 1.35;
}

.token-alt {
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px dashed rgba(180, 83, 9, 0.35);
}

.token-ok {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius);
  border: 1px solid rgba(35, 134, 91, 0.35);
  background: var(--success-dim);
  color: var(--success);
  font-size: 0.82rem;
}

.btn.linkish {
  background: transparent;
  border: none;
  color: var(--muted);
  text-decoration: underline;
  cursor: pointer;
  font-size: 0.78rem;
}

.topbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: space-between;
  align-items: end;
  flex-shrink: 0;
}

.fields {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  flex: 1;
}

.fields label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--muted);
  font-size: 0.75rem;
  min-width: 120px;
}

.fields label.grow {
  flex: 1;
  min-width: 200px;
}

.actions {
  display: flex;
  gap: 8px;
}

.workspace {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 10px;
  overflow: hidden;
}

.file-tree {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
  overflow: auto;
  padding: 8px;
}

.tree-title {
  margin: 0 0 8px;
  font-size: 0.78rem;
  color: var(--muted);
  font-weight: 600;
}

.file-item {
  display: block;
  width: 100%;
  text-align: left;
  border: none;
  background: transparent;
  color: var(--ink);
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.75rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-item:hover,
.file-item.active {
  background: var(--accent-dim);
}

.editor-pane {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: hidden;
}

.ai-bar,
.save-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-shrink: 0;
}

.grow {
  flex: 1;
}

.check {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 0.82rem;
  white-space: nowrap;
}

.monaco-host {
  flex: 1;
  min-height: 240px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: hidden;
}

.hitl-warn {
  margin: 8px 0 0;
  padding: 8px 10px;
  font-size: 0.78rem;
  line-height: 1.45;
  color: var(--warn);
  border: 1px solid rgba(224, 168, 74, 0.3);
  border-radius: 8px;
  background: rgba(224, 168, 74, 0.08);
}

.status {
  margin: 0;
  font-size: 0.85rem;
  color: var(--accent);
}

.status.err {
  color: var(--danger);
}

.meta {
  margin: 0;
  color: var(--muted);
  font-size: 0.78rem;
}

.meta a {
  margin-left: 10px;
}

.hint {
  color: var(--muted);
  font-size: 0.78rem;
}

@media (max-width: 900px) {
  .workspace {
    grid-template-columns: 1fr;
  }

  .file-tree {
    max-height: 140px;
  }
}
</style>
